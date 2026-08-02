"""Управление Xray — генерация конфигов, VLESS-ссылок, перезагрузка ядра."""

import json
import logging
import os
import subprocess
import uuid as _uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.models import Server, VPNKey

logger = logging.getLogger(__name__)

DOMAIN = os.getenv("DOMAIN", "vpn.example.com")
XRAY_CONFIG_PATH = os.getenv("XRAY_CONFIG_PATH", "/etc/xray/config.json")

# Серверы каскада
RU_SERVER_IP = os.getenv("RU_SERVER_IP", "")
NL_SERVER_IP = os.getenv("NL_SERVER_IP", "")
RU_SERVER_DOMAIN = os.getenv("RU_SERVER_DOMAIN", DOMAIN)
NL_SERVER_DOMAIN = os.getenv("NL_SERVER_DOMAIN", "")

# Транзит NL → RU (сервер-сервер через REALITY)
# RU-транзит (REALITY inbound на RU-сервере) слушает 443 — см. xray/xray-ru.json
# и scripts/setup-ru-server.sh. Дефолт ДОЛЖЕН совпадать с remote_xray.py.
RU_TRANSIT_UUID = os.getenv("RU_TRANSIT_UUID", "")
RU_TRANSIT_PORT = int(os.getenv("RU_TRANSIT_PORT", "443"))
RU_TRANSIT_PUBLIC_KEY = os.getenv("RU_TRANSIT_PUBLIC_KEY", "")
RU_TRANSIT_SHORT_ID = os.getenv("RU_TRANSIT_SHORT_ID", "aabbccdd")
RU_TRANSIT_SN = os.getenv("RU_TRANSIT_SN", "www.google.com")

# Cloudflare WARP (WireGuard outbound) — заполняется scripts/06-setup-warp.sh.
# Нужен потому, что гео-база Google метит наш IPv4 как RU (поведенческий сигнал:
# через выход ходят почти только российские пользователи). Все остальные гео-базы
# видят NL. Правится только сменой выхода для google/youtube — см. WARP_DOMAINS.
WARP_PRIVATE_KEY = os.getenv("WARP_PRIVATE_KEY", "")
WARP_PUBLIC_KEY = os.getenv("WARP_PUBLIC_KEY", "")
WARP_ADDRESS_V4 = os.getenv("WARP_ADDRESS_V4", "")
WARP_ADDRESS_V6 = os.getenv("WARP_ADDRESS_V6", "")
WARP_ENDPOINT = os.getenv("WARP_ENDPOINT", "162.159.192.1:2408")
WARP_RESERVED = os.getenv("WARP_RESERVED", "0,0,0")
WARP_MTU = int(os.getenv("WARP_MTU", "1280"))
# IPv6 внутри туннеля WARP. По умолчанию ВЫКЛЮЧЕН и включать не нужно.
# Контейнер xray живёт в docker-сети backend, где IPv6 нет. Если объявить
# адрес /128 и allowedIPs ::/0, Xray резолвит домены через IPv6-резолвер
# 2606:4700:4700::1001 внутри туннеля и получает i/o timeout, а при AAAA-записи
# эндпоинта — "sendto: network is unreachable". Внешне это выглядит так, будто
# WARP молча не пропускает трафик.
WARP_IPV6 = os.getenv("WARP_IPV6", "0").strip().lower() in ("1", "true", "yes", "on")
# Что заворачивать в WARP. Через запятую, синтаксис routing-правил Xray.
#
# ВНИМАНИЕ: не добавляйте сюда "geosite:google" целиком. В эту категорию входят
# connectivitycheck.gstatic.com, dns.google, googleapis.com и mtalk.google.com
# (FCM-пуши). Android определяет наличие интернета запросом к connectivitycheck:
# если WARP отвалится, телефон пометит соединение как «без доступа в интернет»
# и приложения перестанут работать при живом туннеле. Держим список узким —
# только то, где реально мешает гео-метка RU: поиск и YouTube.
WARP_DOMAINS = os.getenv(
    "WARP_DOMAINS",
    "geosite:youtube,domain:googlevideo.com,domain:ytimg.com,domain:ggpht.com,domain:www.google.com",
)

# Порты
VLESS_WS_PORT = int(os.getenv("VLESS_WS_PORT", "443"))
REALITY_PORT = int(os.getenv("REALITY_PORT", "2053"))

# REALITY параметры
REALITY_DEST = os.getenv("REALITY_DEST", "www.samsung.com:443")
REALITY_SERVER_NAMES = os.getenv("REALITY_SERVER_NAMES", "www.samsung.com,samsung.com")
REALITY_PUBLIC_KEY = os.getenv("REALITY_PUBLIC_KEY", "")
REALITY_PRIVATE_KEY = os.getenv("REALITY_PRIVATE_KEY", "")
REALITY_SHORT_ID = os.getenv("REALITY_SHORT_ID", "")

# Адрес gRPC stats API Xray — в Docker xray-контейнер доступен по hostname "xray"
XRAY_API_ADDR = os.getenv("XRAY_API_ADDR", "xray:10085")

# Режим XHTTP. ВАЖНО: значение на сервере (inbound) и у клиента (ссылка/outbound)
# должно совпадать, иначе соединение не установится. "auto" совместим с любыми
# клиентами; "stream-up"/"packet-up" требуют, чтобы клиент явно использовал тот же
# режим (мы пробрасываем его в ссылку параметром &mode=).
XHTTP_MODE = os.getenv("XHTTP_MODE", "auto")

GB = 1_073_741_824


def generate_uuid() -> str:
    return str(_uuid.uuid4())


def build_xray_config(db: Session) -> dict[str, Any]:
    """Собрать полный xray config.json из активных ключей в базе."""
    active_keys = db.query(VPNKey).filter(
        VPNKey.is_active == True  # noqa: E712
    ).all()

    # Фильтруем просроченные и перелимитные
    valid_keys = [k for k in active_keys if k.status == "active"]

    # Строим маппинг speed_limit_kbps → policy level
    # Level 0 = без ограничений; 1, 2, ... = конкретные лимиты (kbps)
    speed_limits: list[int] = sorted({
        k.speed_limit_kbps for k in valid_keys
        if k.speed_limit_kbps and k.speed_limit_kbps > 0
    })
    speed_level_map: dict[int, int] = {kbps: idx + 1 for idx, kbps in enumerate(speed_limits)}

    def _level(key: VPNKey) -> int:
        if key.speed_limit_kbps and key.speed_limit_kbps > 0:
            return speed_level_map.get(key.speed_limit_kbps, 0)
        return 0

    # Клиенты для VLESS inbound
    vless_clients = []
    for key in valid_keys:
        if key.protocol == "vless":
            vless_clients.append({
                "id": key.uuid,
                "email": key.uuid,
                "flow": "",
                "level": _level(key)
            })

    # Клиенты для VLESS REALITY
    reality_clients = []
    for key in valid_keys:
        if key.protocol == "vless":
            reality_clients.append({
                "id": key.uuid,
                "email": key.uuid,
                "flow": "xtls-rprx-vision",
                "level": _level(key)
            })

    # Строим policy levels: 0 = без ограничений, 1+ = ограниченные
    policy_levels: dict[str, Any] = {
        "0": {
            "statsUserUplink": True,
            "statsUserDownlink": True,
            "handshake": 8,
            "connIdle": 300,
            "uplinkOnly": 10,
            "downlinkOnly": 15,
        }
    }
    for kbps, lvl in speed_level_map.items():
        # bufferSize в KB используется как маркер; реальное ограничение
        # скорости требует OS-level tc или внешнего rate limiter.
        policy_levels[str(lvl)] = {
            "statsUserUplink": True,
            "statsUserDownlink": True,
            "handshake": 8,
            "connIdle": 300,
            "uplinkOnly": 10,
            "downlinkOnly": 15,
            "bufferSize": max(kbps // 8, 4),  # KB/s hint (не enforcement)
        }

    config: dict[str, Any] = {
        "log": {
            "loglevel": "warning",
            "access": "/var/log/xray/access.log",
            "error": "/var/log/xray/error.log"
        },
        "api": {
            "tag": "api",
            "services": ["StatsService"]
        },
        "stats": {},
        "policy": {
            "levels": policy_levels,
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True,
                "statsOutboundUplink": True,
                "statsOutboundDownlink": True
            }
        },
        "inbounds": [
            {
                "tag": "api-inbound",
                "listen": "0.0.0.0",
                "port": 10085,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"}
            },
            {
                "tag": "VLESS-WS",
                "listen": "0.0.0.0",
                "port": 8443,
                "protocol": "vless",
                "settings": {
                    "clients": vless_clients,
                    "decryption": "none"
                },
                "streamSettings": {
                    "network": "ws",
                    "security": "none",
                    "wsSettings": {
                        "path": "/vless-ws",
                        "heartbeatPeriod": 30
                    }
                },
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"]
                }
            },
            {
                "tag": "VLESS-XHTTP",
                "listen": "0.0.0.0",
                "port": 8444,
                "protocol": "vless",
                "settings": {
                    "clients": vless_clients,
                    "decryption": "none"
                },
                "streamSettings": {
                    "network": "xhttp",
                    "security": "none",
                    "xhttpSettings": {
                        "path": "/xhttp",
                        "mode": XHTTP_MODE,
                        "xPaddingBytes": "100-1000"
                    }
                },
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"]
                }
            },
            {
                "tag": "VLESS-gRPC",
                "listen": "0.0.0.0",
                "port": 8445,
                "protocol": "vless",
                "settings": {
                    "clients": vless_clients,
                    "decryption": "none"
                },
                "streamSettings": {
                    "network": "grpc",
                    "security": "none",
                    "grpcSettings": {
                        "serviceName": "VpnService",
                        "multiMode": False
                    }
                },
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"]
                }
            }
        ],
        "outbounds": [
            {
                "tag": "DIRECT",
                "protocol": "freedom",
                "settings": {
                    "domainStrategy": "UseIP",
                    "packetEncoding": "xudp"
                }
            },
            {
                "tag": "BLACKHOLE",
                "protocol": "blackhole"
            }
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["api-inbound"],
                    "outboundTag": "api"
                },
                {
                    "type": "field",
                    "outboundTag": "BLACKHOLE",
                    "protocol": ["bittorrent"]
                },
                {
                    "type": "field",
                    "outboundTag": "DIRECT",
                    "network": "tcp,udp"
                }
            ]
        }
    }

    # Добавляем REALITY inbound если ключи настроены
    if REALITY_PRIVATE_KEY and REALITY_SHORT_ID:
        config["inbounds"].append({
            "tag": "VLESS-REALITY",
            "listen": "0.0.0.0",
            "port": 443,
            "protocol": "vless",
            "settings": {
                "clients": reality_clients,
                "decryption": "none"
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": REALITY_DEST,
                    "xver": 0,
                    "serverNames": [s.strip() for s in REALITY_SERVER_NAMES.split(",")],
                    "privateKey": REALITY_PRIVATE_KEY,
                    "shortIds": [REALITY_SHORT_ID]
                }
            },
            "sniffing": {
                "enabled": True,
                "destOverride": ["http", "tls", "quic"]
            }
        })

    # Google/YouTube через Cloudflare WARP.
    # ВАЖНО: правило должно стоять ВЫШЕ правил каскада. Google считает наш IPv4
    # российским и отдаёт для *.googlevideo.com адреса GGC-кэшей внутри российских
    # AS. Эти адреса матчатся на "geoip:ru" и без этого правила YouTube уходил бы
    # в RU-хаб — то есть выходил бы в реальный российский IP.
    if WARP_PRIVATE_KEY and WARP_PUBLIC_KEY and WARP_ADDRESS_V4:
        # По умолчанию туннель строго IPv4 — см. комментарий у WARP_IPV6
        warp_address = [f"{WARP_ADDRESS_V4}/32"]
        warp_allowed_ips = ["0.0.0.0/0"]
        if WARP_IPV6:
            if ":" in WARP_ADDRESS_V6:
                warp_address.append(f"{WARP_ADDRESS_V6}/128")
                warp_allowed_ips.append("::/0")
            elif WARP_ADDRESS_V6:
                # Битый .env (например v4-адрес в поле v6) не должен ломать outbound
                logger.warning("WARP_ADDRESS_V6=%r не похож на IPv6 — пропускаю", WARP_ADDRESS_V6)

        try:
            reserved = [int(x) for x in WARP_RESERVED.split(",") if x.strip() != ""]
        except ValueError:
            logger.warning("WARP_RESERVED=%r не парсится — использую [0,0,0]", WARP_RESERVED)
            reserved = [0, 0, 0]
        if len(reserved) != 3:
            logger.warning("WARP_RESERVED=%r не 3 байта — использую [0,0,0]", WARP_RESERVED)
            reserved = [0, 0, 0]

        config["outbounds"].append({
            "tag": "WARP",
            "protocol": "wireguard",
            "settings": {
                "secretKey": WARP_PRIVATE_KEY,
                "address": warp_address,
                "peers": [{
                    "publicKey": WARP_PUBLIC_KEY,
                    "allowedIPs": warp_allowed_ips,
                    "endpoint": WARP_ENDPOINT
                }],
                "reserved": reserved,
                "mtu": WARP_MTU
            }
        })

        warp_domains = [d.strip() for d in WARP_DOMAINS.split(",") if d.strip()]
        if warp_domains:
            rules = config["routing"]["rules"]
            catchall = rules.pop()  # убираем catch-all (tcp,udp → DIRECT)
            rules.append({
                "type": "field",
                "outboundTag": "WARP",
                "domain": warp_domains
            })
            rules.append(catchall)

    # Каскад: NL → RU для российского трафика
    if RU_SERVER_IP and RU_TRANSIT_UUID and RU_TRANSIT_PUBLIC_KEY:
        config["outbounds"].append({
            "tag": "RU-PROXY",
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": RU_SERVER_IP,
                    "port": RU_TRANSIT_PORT,
                    "users": [{
                        "id": RU_TRANSIT_UUID,
                        "encryption": "none",
                        "flow": "xtls-rprx-vision"
                    }]
                }]
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "fingerprint": "chrome",
                    "serverName": RU_TRANSIT_SN,
                    "publicKey": RU_TRANSIT_PUBLIC_KEY,
                    "shortId": RU_TRANSIT_SHORT_ID
                }
            },
            "mux": {"enabled": False}
        })

        # Вставляем RU-правила перед catch-all DIRECT правилом
        rules = config["routing"]["rules"]
        catchall = rules.pop()  # убираем catch-all (tcp,udp → DIRECT)
        rules.extend([
            {
                "type": "field",
                "outboundTag": "RU-PROXY",
                "domain": [
                    "regexp:\\.ru$",
                    "regexp:\\.su$",
                    "domain:yandex.com",
                    "domain:yandex.ru",
                    "domain:mail.ru",
                    "domain:vk.com",
                    "domain:ok.ru",
                    "domain:sberbank.ru",
                    "domain:gosuslugi.ru",
                    "domain:nalog.gov.ru",
                    "domain:mos.ru",
                    "domain:rt.ru",
                    "domain:tinkoff.ru",
                    "domain:wildberries.ru",
                    "domain:ozon.ru",
                    "domain:avito.ru",
                    "domain:1c.ru"
                ]
            },
            {
                "type": "field",
                "outboundTag": "RU-PROXY",
                "ip": ["geoip:ru"]
            }
        ])
        rules.append(catchall)

    return config


def write_xray_config(db: Session) -> bool:
    """Пересобрать и записать конфиг Xray.

    Возвращает True если конфиг изменился (нужна перезагрузка),
    False если содержимое идентично текущему файлу.
    """
    config = build_xray_config(db)
    new_content = json.dumps(config, indent=2, ensure_ascii=False)
    config_path = Path(XRAY_CONFIG_PATH)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if config_path.exists() and config_path.read_text(encoding="utf-8") == new_content:
            logger.debug("Xray config не изменился — перезапись пропущена")
            return False
    except Exception:
        pass

    config_path.write_text(new_content, encoding="utf-8")
    logger.info("Xray config записан: %s", XRAY_CONFIG_PATH)
    return True


def _docker_restart_via_socket(container_name: str, stop_timeout: int = 5) -> bool:
    """Перезапустить Docker-контейнер через REST API (Unix socket) без docker CLI.

    Работает в python:slim-образах без установленного docker CLI.
    """
    import http.client
    import socket as _socket
    import urllib.parse as _up

    DOCKER_SOCK = "/var/run/docker.sock"

    class _UnixConn(http.client.HTTPConnection):
        def connect(self):
            self.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(DOCKER_SOCK)

    try:
        enc = _up.quote(container_name, safe="")
        conn = _UnixConn("localhost", timeout=20)
        conn.request(
            "POST",
            f"/containers/{enc}/restart?t={stop_timeout}",
            headers={"Content-Length": "0", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        body = resp.read(300).decode("utf-8", errors="replace")
        conn.close()
        if resp.status == 204:
            return True
        # 404 = контейнер не найден, 409 = занят
        logger.warning(
            "Docker API restart: HTTP %d для %s — %s",
            resp.status, container_name, body[:150],
        )
        return False
    except Exception as e:
        logger.warning("Docker socket restart failed для %s: %s", container_name, e)
        return False


def reload_xray() -> bool:
    """Перезагрузить Xray.

    Порядок попыток:
    1. Docker socket API: полный restart контейнера — надёжное применение конфига
    2. docker CLI restart — если CLI установлен в контейнере
    3. systemctl restart xray — bare metal
    4. killall -HUP xray — bare metal без systemd

    Примечание: SIGHUP-«hot reload» не используется. Xray-core не умеет
    перечитывать конфиг по сигналу и не ставит обработчик SIGHUP, а в контейнере
    он стартует как PID 1 (`exec xray run`) — ядро Linux игнорирует сигналы без
    обработчика, отправленные PID 1. Поэтому единственный надёжный способ
    применить новый конфиг — перезапуск контейнера.
    """
    docker_sock = "/var/run/docker.sock"
    container_name = os.getenv("XRAY_CONTAINER_NAME", "ufobzk-xray")

    if os.path.exists(docker_sock):
        # ── Попытка 1: полный restart через Docker socket API ──
        if _docker_restart_via_socket(container_name):
            logger.info("Xray перезагружен через Docker API (socket)")
            return True

        # ── Попытка 2: docker CLI (fallback — если установлен) ──
        try:
            result = subprocess.run(
                ["docker", "restart", "-t", "5", container_name],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                logger.info("Xray перезагружен через docker CLI")
                return True
            logger.warning("docker restart код %s: %s", result.returncode, result.stderr.strip())
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("docker CLI не сработал: %s", e)

    # ── Попытка 3: systemctl (bare metal) ──
    try:
        result = subprocess.run(
            ["systemctl", "restart", "xray"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            logger.info("Xray перезагружен через systemctl")
            return True
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.debug("systemctl недоступен: %s", e)

    # ── Попытка 4: killall -HUP (bare metal без systemd) ──
    try:
        subprocess.run(["killall", "-HUP", "xray"], capture_output=True, timeout=5)
        logger.info("Xray получил HUP-сигнал")
        return True
    except Exception as e:
        logger.warning("Все методы перезагрузки Xray не сработали: %s", e)
        return False


def _docker_exec_via_socket(container_name: str, cmd: list[str], timeout: int = 10) -> bool:
    """Выполнить команду внутри контейнера через Docker exec API (Unix socket).

    Возвращает True если команда завершилась с кодом 0.
    """
    import http.client as _hc
    import json as _json
    import socket as _sk
    import urllib.parse as _up

    DOCKER_SOCK = "/var/run/docker.sock"

    class _UnixConn(_hc.HTTPConnection):
        def connect(self):
            self.sock = _sk.socket(_sk.AF_UNIX, _sk.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(DOCKER_SOCK)

    enc = _up.quote(container_name, safe="")
    try:
        # 1. Создать exec-инстанс
        create_body = _json.dumps({
            "AttachStdout": True, "AttachStderr": True, "Cmd": cmd,
        }).encode()
        conn = _UnixConn("localhost", timeout=timeout)
        conn.request("POST", f"/containers/{enc}/exec", body=create_body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(create_body))})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status not in (200, 201):
            logger.debug("Docker exec create: HTTP %d — %s", resp.status, data[:150])
            return False
        exec_id = _json.loads(data).get("Id")
        if not exec_id:
            return False

        # 2. Запустить exec (синхронно)
        start_body = _json.dumps({"Detach": False, "Tty": False}).encode()
        conn = _UnixConn("localhost", timeout=timeout)
        conn.request("POST", f"/exec/{exec_id}/start", body=start_body,
                     headers={"Content-Type": "application/json",
                              "Content-Length": str(len(start_body))})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status != 200:
            logger.debug("Docker exec start: HTTP %d", resp.status)
            return False

        # 3. Проверить exit code
        conn = _UnixConn("localhost", timeout=timeout)
        conn.request("GET", f"/exec/{exec_id}/json", headers={"Content-Length": "0"})
        resp = conn.getresponse()
        inspect = resp.read()
        conn.close()
        if resp.status == 200:
            return _json.loads(inspect).get("ExitCode") == 0
        return True
    except Exception as e:
        logger.debug("Docker exec failed для %s: %s", container_name, e)
        return False


def _nginx_graceful_reload() -> None:
    """Graceful reload nginx (`nginx -s reload`) после перезапуска xray.

    После рестарта xray старые keepalive-соединения nginx→xray закрыты.
    `nginx -s reload` поднимает новые воркеры с чистыми соединениями к xray,
    не разрывая существующие клиентские соединения (старые воркеры доживают).

    Используется Docker exec API, а не сигнал контейнеру: в nginx-контейнере
    PID 1 — это `sh` (nginx запущен без `exec`), поэтому сигнал контейнеру
    до мастер-процесса nginx не дойдёт.
    """
    nginx_name = os.getenv("NGINX_CONTAINER_NAME", "ufobzk-nginx")
    docker_sock = "/var/run/docker.sock"
    if not os.path.exists(docker_sock):
        return
    import time as _t
    _t.sleep(3)  # даём xray время подняться перед тем как nginx переподключится
    if _docker_exec_via_socket(nginx_name, ["nginx", "-s", "reload"]):
        logger.info("Nginx gracefully reloaded (nginx -s reload) после перезапуска Xray")
    else:
        logger.debug("Nginx reload не выполнен — соединения к xray переустановятся сами")


def sync_and_reload(db: Session) -> bool:
    """Пересобрать конфиг и перезагрузить Xray (только если конфиг изменился)."""
    changed = write_xray_config(db)
    if not changed:
        logger.debug("Xray конфиг не изменился — перезагрузка не нужна")
        return True
    reloaded = reload_xray()
    if reloaded:
        _nginx_graceful_reload()
    return reloaded


# ── Генерация ссылок подключения ──


def build_vless_ws_link(key: VPNKey, server_domain: str, port: int = 443, remark: str = "") -> str:
    """VLESS WebSocket + TLS ссылка."""
    if not remark:
        remark = f"{key.name}@{server_domain}"
    params = (
        f"type=ws&security=tls&host={server_domain}"
        f"&path=%2Fvless-ws&sni={server_domain}"
        f"&fp=chrome&alpn=http%2F1.1"
    )
    return f"vless://{key.uuid}@{server_domain}:{port}?{params}#{quote(remark)}"


def build_vless_xhttp_link(key: VPNKey, server_domain: str, port: int = 443, remark: str = "") -> str:
    """VLESS XHTTP + TLS ссылка (новый транспорт, лучше WS для DPI)."""
    if not remark:
        remark = f"{key.name}@{server_domain}-xhttp"
    params = (
        f"type=xhttp&security=tls&host={server_domain}"
        f"&path=%2Fxhttp&mode={XHTTP_MODE}&sni={server_domain}"
        f"&fp=chrome&alpn=h2%2Chttp%2F1.1"
    )
    return f"vless://{key.uuid}@{server_domain}:{port}?{params}#{quote(remark)}"


def build_vless_reality_link(key: VPNKey, server_ip: str, port: int = 443, remark: str = "") -> str:
    """VLESS REALITY ссылка."""
    if not remark:
        remark = f"{key.name}-reality"
    server_names = REALITY_SERVER_NAMES.split(",")[0].strip()
    params = (
        f"type=tcp&security=reality&sni={server_names}"
        f"&fp=chrome&pbk={REALITY_PUBLIC_KEY}"
        f"&sid={REALITY_SHORT_ID}&flow=xtls-rprx-vision"
    )
    return f"vless://{key.uuid}@{server_ip}:{port}?{params}#{quote(remark)}"


def build_vless_grpc_link(key: VPNKey, server_domain: str, port: int = 443, remark: str = "") -> str:
    """VLESS gRPC + TLS ссылка (HTTP/2, сложнее fingerprint для DPI)."""
    if not remark:
        remark = f"{key.name}@{server_domain}-grpc"
    params = (
        f"type=grpc&security=tls&host={server_domain}"
        f"&serviceName=VpnService&sni={server_domain}"
        f"&fp=chrome&alpn=h2"
    )
    return f"vless://{key.uuid}@{server_domain}:{port}?{params}#{quote(remark)}"


def get_user_links(key: VPNKey) -> list[dict[str, str]]:
    """Получить ссылки подключения для ключа (основной NL-сервер).

    RU-сервер намеренно исключён: он используется только как EXIT-узел
    каскада для российского трафика (NL→RU outbound). Прямые подключения
    пользователей к RU-серверу не нужны и не будут работать корректно.
    Remote серверы (Finland, NL-2, …) добавляются через get_all_links.
    """
    links = []

    # Основной сервер (управляющий) — WS/XHTTP/gRPC через CDN+nginx
    if DOMAIN:
        links.append({
            "name": f"🇳🇱 Европа (WS+TLS)",
            "link": build_vless_ws_link(key, DOMAIN, VLESS_WS_PORT, f"NL-{key.name}"),
            "type": "vless-ws"
        })
        links.append({
            "name": f"\U0001f1f3\U0001f1f1 Европа (XHTTP+TLS)",
            "link": build_vless_xhttp_link(key, DOMAIN, VLESS_WS_PORT, f"NL-XHTTP-{key.name}"),
            "type": "vless-xhttp"
        })
        links.append({
            "name": f"🇳🇱 Европа (gRPC+TLS)",
            "link": build_vless_grpc_link(key, DOMAIN, VLESS_WS_PORT, f"NL-gRPC-{key.name}"),
            "type": "vless-grpc"
        })

    # NL сервер через REALITY (прямое подключение, обходит DPI)
    if NL_SERVER_IP and REALITY_PUBLIC_KEY:
        links.append({
            "name": f"🇳🇱 Европа (REALITY)",
            "link": build_vless_reality_link(key, NL_SERVER_IP, REALITY_PORT, f"NL-Reality-{key.name}"),
            "type": "vless-reality"
        })

    return links


import re as _re

_IP_RE = _re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$')


def _host_is_ip(host: str) -> bool:
    return bool(_IP_RE.match(host))


def get_server_links(key: VPNKey, server: Server) -> list[dict[str, str]]:
    """Ссылки для ключа на конкретном remote сервере.

    Если host — IP адрес (нет nginx/TLS): только REALITY (работает без nginx).
    Если host — доменное имя: WS/XHTTP/gRPC+TLS + REALITY.
    """
    links = []
    host = server.host
    ws_port = server.ws_port or 443
    reality_port = server.reality_port or 2053
    suffix = server.name.replace(" ", "-")
    is_ip = _host_is_ip(host)

    if not is_ip:
        # Домен с nginx/TLS — WS/XHTTP/gRPC работают
        links.append({
            "name": f"{server.region.upper()}-{suffix} (WS+TLS)",
            "link": build_vless_ws_link(key, host, ws_port, f"{suffix}-{key.name}"),
            "type": "vless-ws",
            "server_id": server.id,
            "region": server.region,
        })
        links.append({
            "name": f"{server.region.upper()}-{suffix} (XHTTP+TLS)",
            "link": build_vless_xhttp_link(key, host, ws_port, f"{suffix}-XHTTP-{key.name}"),
            "type": "vless-xhttp",
            "server_id": server.id,
            "region": server.region,
        })
        links.append({
            "name": f"{server.region.upper()}-{suffix} (gRPC+TLS)",
            "link": build_vless_grpc_link(key, host, ws_port, f"{suffix}-gRPC-{key.name}"),
            "type": "vless-grpc",
            "server_id": server.id,
            "region": server.region,
        })

    # REALITY — работает на IP напрямую, не требует nginx
    if server.reality_public_key and server.reality_short_id:
        sn = (server.reality_server_names or "www.samsung.com").split(",")[0].strip()
        remark = f"{suffix}-Reality-{key.name}"
        params = (
            f"type=tcp&security=reality&sni={sn}"
            f"&fp=chrome&pbk={server.reality_public_key}"
            f"&sid={server.reality_short_id}&flow=xtls-rprx-vision"
        )
        from urllib.parse import quote as _quote
        link = f"vless://{key.uuid}@{host}:{reality_port}?{params}#{_quote(remark)}"
        links.append({
            "name": f"{server.region.upper()}-{suffix} (REALITY)",
            "link": link,
            "type": "vless-reality",
            "server_id": server.id,
            "region": server.region,
        })
    return links


def get_all_links(db: Session, key: VPNKey) -> list[dict[str, str]]:
    """Все ссылки для ключа: локальный сервер + все remote серверы из БД."""
    links = get_user_links(key)
    # Добавляем server_id и region для локальных ссылок
    for link in links:
        link.setdefault("server_id", None)
        link.setdefault("region", "main")

    remote_servers = db.query(Server).filter(Server.is_active == True).order_by(Server.priority).all()  # noqa: E712
    for server in remote_servers:
        links.extend(get_server_links(key, server))
    return links


def get_subscription_content(keys: list[VPNKey], db: Session | None = None) -> str:
    """Собрать base64-подписку из всех ключей пользователя.
    Если db передана — включает remote серверы.
    """
    import base64
    all_links = []
    for key in keys:
        if key.status != "active":
            continue
        if db is not None:
            link_list = get_all_links(db, key)
        else:
            link_list = get_user_links(key)
        for link_info in link_list:
            if link_info["type"] in ("vless-ws", "vless-xhttp", "vless-grpc", "vless-reality"):
                all_links.append(link_info["link"])
    return base64.b64encode("\n".join(all_links).encode()).decode()


def _parse_link_host_port(link: str, default_port: int) -> tuple[str, int]:
    """Извлечь (host, port) из VLESS URI."""
    try:
        after_at = link.split("@", 1)[1]  # "host:port?params#tag"
        host_port = after_at.split("?", 1)[0]  # "host:port"
        # IPv6: [::1]:443
        if host_port.startswith("["):
            host = host_port.split("]")[0][1:]
            port = int(host_port.split("]:")[1])
        else:
            parts = host_port.rsplit(":", 1)
            host = parts[0]
            port = int(parts[1]) if len(parts) == 2 else default_port
        return host, port
    except Exception:
        return DOMAIN, default_port


def _parse_link_params(link: str) -> dict[str, str]:
    """Извлечь query-параметры (sni, pbk, sid, path, flow, ...) из VLESS URI."""
    from urllib.parse import parse_qs
    if "?" not in link:
        return {}
    query = link.split("?", 1)[1].split("#", 1)[0]
    return {k: v[0] for k, v in parse_qs(query).items()}


def _build_outbound_for_link(key: VPNKey, link_info: dict[str, Any]) -> dict[str, Any] | None:
    """Построить outbound JSON для одной ссылки."""
    server_host, port = _parse_link_host_port(link_info["link"], VLESS_WS_PORT)
    if link_info["type"] == "vless-reality":
        p = _parse_link_params(link_info["link"])
        return {
            "tag": link_info["name"],
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": server_host,
                    "port": port,
                    "users": [{
                        "id": key.uuid,
                        "encryption": "none",
                        "flow": p.get("flow", "xtls-rprx-vision"),
                    }]
                }]
            },
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "serverName": p.get("sni", ""),
                    "publicKey": p.get("pbk", ""),
                    "shortId": p.get("sid", ""),
                    "fingerprint": p.get("fp", "chrome"),
                },
            },
        }
    elif link_info["type"] == "vless-xhttp":
        p = _parse_link_params(link_info["link"])
        return {
            "tag": link_info["name"],
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": server_host,
                    "port": port,
                    "users": [{"id": key.uuid, "encryption": "none"}]
                }]
            },
            "streamSettings": {
                "network": "xhttp",
                "security": "tls",
                "tlsSettings": {"serverName": p.get("sni", server_host), "fingerprint": "chrome"},
                "xhttpSettings": {"path": p.get("path", "/xhttp"), "mode": p.get("mode", XHTTP_MODE)},
            },
        }
    elif link_info["type"] == "vless-ws":
        return {
            "tag": link_info["name"],
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": server_host,
                    "port": port,
                    "users": [{"id": key.uuid, "encryption": "none"}]
                }]
            },
            "streamSettings": {
                "network": "ws",
                "security": "tls",
                "tlsSettings": {"serverName": server_host, "fingerprint": "chrome"},
                "wsSettings": {"path": "/vless-ws"}
            },
            "mux": {"enabled": True, "concurrency": 8, "xudpConcurrency": 16, "packetEncoding": "xudp"}
        }
    elif link_info["type"] == "vless-grpc":
        return {
            "tag": link_info["name"],
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": server_host,
                    "port": port,
                    "users": [{"id": key.uuid, "encryption": "none"}]
                }]
            },
            "streamSettings": {
                "network": "grpc",
                "security": "tls",
                "tlsSettings": {"serverName": server_host, "fingerprint": "chrome"},
                "grpcSettings": {"serviceName": "VpnService", "multiMode": False}
            },
            "mux": {"enabled": True, "concurrency": 8, "xudpConcurrency": 16, "packetEncoding": "xudp"}
        }
    return None


def get_subscription_json(keys: list[VPNKey], db: Session | None = None) -> dict[str, Any]:
    """JSON подписка для продвинутых клиентов (v2rayNG, Hiddify, Nekoray).
    Включает mux + fingerprint + каскад routing + fallback балансировку.
    """
    outbounds = []
    eu_tags = []
    ru_tags = []

    for key in keys:
        if key.status != "active":
            continue
        if db is not None:
            link_list = get_all_links(db, key)
        else:
            link_list = get_user_links(key)

        for link_info in link_list:
            ob = _build_outbound_for_link(key, link_info)
            if ob:
                outbounds.append(ob)
                region = link_info.get("region", "main")
                if region == "ru":
                    ru_tags.append(ob["tag"])
                else:
                    eu_tags.append(ob["tag"])

    # Balancer + routing для каскада и fallback
    routing_rules = [
        {"type": "field", "outboundTag": "BLOCK", "protocol": ["bittorrent"]},
    ]

    # RU трафик → RU серверы (каскад)
    if ru_tags:
        routing_rules.append({
            "type": "field",
            "outboundTag": ru_tags[0],
            "domain": ["regexp:\\.ru$", "regexp:\\.su$", "geosite:ru"],
        })
        routing_rules.append({
            "type": "field",
            "outboundTag": ru_tags[0],
            "ip": ["geoip:ru"],
        })

    # Остальной трафик → ближайший EU сервер (первый в списке = приоритетный)
    if eu_tags:
        routing_rules.append({
            "type": "field",
            "outboundTag": eu_tags[0],
            "network": "tcp,udp",
        })

    result = {
        "outbounds": outbounds + [
            {"tag": "DIRECT", "protocol": "freedom", "settings": {"domainStrategy": "UseIP", "packetEncoding": "xudp"}},
            {"tag": "BLOCK", "protocol": "blackhole"},
        ],
        "log": {"loglevel": "warning"},
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": routing_rules,
        }
    }

    # Если несколько EU серверов — добавляем balancer
    if len(eu_tags) > 1:
        result["routing"]["balancers"] = [
            {
                "tag": "balancer-eu",
                "selector": eu_tags,
                "strategy": {"type": "random"},
            }
        ]
        # Заменяем правило для EU на balancer
        for rule in result["routing"]["rules"]:
            if rule.get("outboundTag") == eu_tags[0] and rule.get("network") == "tcp,udp":
                rule.pop("outboundTag")
                rule["balancerTag"] = "balancer-eu"

    return result


def _accumulate_user_stat(totals: dict[str, int], name: str, value: int) -> None:
    """Добавить значение счётчика к суммарному трафику пользователя (по UUID).

    Формат name: "user>>>{email}>>>traffic>>>uplink|downlink", где email = UUID.
    """
    parts = name.split(">>>")
    if len(parts) >= 2:
        uuid_part = parts[1].split("@")[0]  # на случай суффикса @tag
        if uuid_part:
            totals[uuid_part] = totals.get(uuid_part, 0) + value


def _parse_xray_stats(output: str) -> dict[str, int]:
    """Разобрать вывод `xray api statsquery` в {uuid: total_bytes}.

    Поддерживает оба формата вывода Xray:
    - JSON (современные версии): {"stat": [{"name": "...", "value": "123"}]}
    - protobuf-text (старые версии): stat { name: "..." value: 123 }
    """
    import re as _re

    output = (output or "").strip()
    if not output:
        return {}

    totals: dict[str, int] = {}

    # ── 1. Современный формат — JSON ──
    if output[0] in "{[":
        try:
            data = json.loads(output)
            stats = data.get("stat") if isinstance(data, dict) else data
            for item in stats or []:
                name = item.get("name", "")
                # protojson сериализует int64 как строку
                value = int(item.get("value", 0) or 0)
                if name:
                    _accumulate_user_stat(totals, name, value)
            return totals
        except (ValueError, TypeError, AttributeError) as e:
            logger.debug("Не удалось разобрать JSON stats, пробую text-формат: %s", e)
            totals.clear()

    # ── 2. Legacy формат — protobuf text ──
    current_name: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        name_m = _re.search(r'name:\s*"([^"]+)"', line)
        if name_m:
            current_name = name_m.group(1)
            continue
        value_m = _re.search(r'value:\s*(\d+)', line)
        if value_m and current_name:
            _accumulate_user_stat(totals, current_name, int(value_m.group(1)))
            current_name = None

    return totals


def get_all_xray_stats() -> dict[str, int]:
    """Получить статистику трафика всех пользователей одним вызовом к Xray stats API.

    Возвращает {uuid: total_bytes} — суммарный трафик (uplink + downlink)
    по всем inbound тегам для каждого пользователя.
    UUID хранится в поле email клиента (client.email == client.id == uuid).
    """
    try:
        result = subprocess.run(
            ["xray", "api", "statsquery",
             f"--server={XRAY_API_ADDR}",
             "-pattern=user>>>"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            logger.debug("Xray statsquery вернул код %d: %s",
                         result.returncode, result.stderr[:200])
            return {}
        return _parse_xray_stats(result.stdout)

    except FileNotFoundError:
        logger.warning("Бинарник xray не найден — статистика недоступна")
        return {}
    except Exception as e:
        logger.debug("Ошибка получения статистики Xray: %s", e)
        return {}


def reset_xray_user_stats(uuid: str) -> bool:
    """Сбросить счётчик трафика Xray для конкретного пользователя.

    Вызывает statsquery с флагом -reset для всех счётчиков данного UUID.
    Возвращает True если сброс прошёл успешно.
    """
    try:
        result = subprocess.run(
            ["xray", "api", "statsquery",
             f"--server={XRAY_API_ADDR}",
             f"-pattern=user>>>{uuid}",
             "-reset"],
            capture_output=True, text=True, timeout=10
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.debug("Ошибка сброса статистики Xray для %s: %s", uuid, e)
        return False
