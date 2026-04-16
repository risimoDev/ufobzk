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

from app.models import VPNKey

logger = logging.getLogger(__name__)

DOMAIN = os.getenv("DOMAIN", "vpn.example.com")
XRAY_CONFIG_PATH = os.getenv("XRAY_CONFIG_PATH", "/etc/xray/config.json")

# Серверы каскада
RU_SERVER_IP = os.getenv("RU_SERVER_IP", "")
NL_SERVER_IP = os.getenv("NL_SERVER_IP", "")
RU_SERVER_DOMAIN = os.getenv("RU_SERVER_DOMAIN", DOMAIN)
NL_SERVER_DOMAIN = os.getenv("NL_SERVER_DOMAIN", "")

# Транзит NL → RU (сервер-сервер через REALITY)
RU_TRANSIT_UUID = os.getenv("RU_TRANSIT_UUID", "")
RU_TRANSIT_PORT = int(os.getenv("RU_TRANSIT_PORT", "8443"))
RU_TRANSIT_PUBLIC_KEY = os.getenv("RU_TRANSIT_PUBLIC_KEY", "")
RU_TRANSIT_SHORT_ID = os.getenv("RU_TRANSIT_SHORT_ID", "aabbccdd")
RU_TRANSIT_SN = os.getenv("RU_TRANSIT_SN", "www.google.com")

# Порты
VLESS_WS_PORT = int(os.getenv("VLESS_WS_PORT", "443"))
REALITY_PORT = int(os.getenv("REALITY_PORT", "2053"))

# REALITY параметры
REALITY_DEST = os.getenv("REALITY_DEST", "www.samsung.com:443")
REALITY_SERVER_NAMES = os.getenv("REALITY_SERVER_NAMES", "www.samsung.com,samsung.com")
REALITY_PUBLIC_KEY = os.getenv("REALITY_PUBLIC_KEY", "")
REALITY_PRIVATE_KEY = os.getenv("REALITY_PRIVATE_KEY", "")
REALITY_SHORT_ID = os.getenv("REALITY_SHORT_ID", "")

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

    # Клиенты для VLESS inbound
    vless_clients = []
    for key in valid_keys:
        if key.protocol == "vless":
            vless_clients.append({
                "id": key.uuid,
                "flow": ""
            })

    # Клиенты для VLESS REALITY
    reality_clients = []
    for key in valid_keys:
        if key.protocol == "vless":
            reality_clients.append({
                "id": key.uuid,
                "flow": "xtls-rprx-vision"
            })

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
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True
                }
            },
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
                "listen": "127.0.0.1",
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
                        "path": "/vless-ws"
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
                "protocol": "freedom"
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
            }
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


def write_xray_config(db: Session) -> None:
    """Пересобрать и записать конфиг Xray."""
    config = build_xray_config(db)
    config_path = Path(XRAY_CONFIG_PATH)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
    logger.info("Xray config записан: %s", XRAY_CONFIG_PATH)


def reload_xray() -> bool:
    """Перезагрузить Xray.

    Порядок попыток:
    1. Docker socket API (production — xray в отдельном контейнере)
    2. systemctl restart xray (bare metal)
    3. killall -HUP xray (bare metal без systemd)
    """
    docker_sock = "/var/run/docker.sock"
    container_name = os.getenv("XRAY_CONTAINER_NAME", "ufobzk-xray")

    # ── Попытка 1: Docker socket ──
    if os.path.exists(docker_sock):
        try:
            import socket as _socket
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(15)
            sock.connect(docker_sock)
            request = (
                f"POST /containers/{container_name}/restart?t=5 HTTP/1.1\r\n"
                f"Host: localhost\r\n"
                f"Content-Length: 0\r\n"
                f"Connection: close\r\n\r\n"
            ).encode()
            sock.sendall(request)
            response = b""
            while True:
                chunk = sock.recv(256)
                if not chunk:
                    break
                response += chunk
            sock.close()
            if b"204" in response or b"200" in response:
                logger.info("Xray перезагружен через Docker socket")
                return True
            logger.warning("Docker socket ответил неожиданно: %s", response[:100])
        except Exception as e:
            logger.warning("Не удалось перезагрузить через Docker socket: %s", e)

    # ── Попытка 2: systemctl (bare metal) ──
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

    # ── Попытка 3: killall -HUP (bare metal без systemd) ──
    try:
        subprocess.run(["killall", "-HUP", "xray"], capture_output=True, timeout=5)
        logger.info("Xray получил HUP-сигнал")
        return True
    except Exception as e:
        logger.debug("killall не сработал: %s", e)
        return False


def sync_and_reload(db: Session) -> bool:
    """Пересобрать конфиг и перезагрузить Xray."""
    write_xray_config(db)
    return reload_xray()


# ── Генерация ссылок подключения ──


def build_vless_ws_link(key: VPNKey, server_domain: str, port: int = 443, remark: str = "") -> str:
    """VLESS WebSocket + TLS ссылка."""
    if not remark:
        remark = f"{key.name}@{server_domain}"
    params = (
        f"type=ws&security=tls&host={server_domain}"
        f"&path=%2Fvless-ws&sni={server_domain}"
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


def get_user_links(key: VPNKey) -> list[dict[str, str]]:
    """Получить все ссылки подключения для ключа.

    Для каскадного VPN возвращаем:
    - RU-сервер (VLESS-WS или REALITY)
    - NL-сервер (VLESS-WS или REALITY)
    """
    links = []

    # Основной сервер (управляющий) — VLESS WS через CDN
    if DOMAIN:
        links.append({
            "name": f"🇳🇱 Европа (WS+TLS)",
            "link": build_vless_ws_link(key, DOMAIN, VLESS_WS_PORT, f"NL-{key.name}"),
            "type": "vless-ws"
        })

    # NL сервер через REALITY
    if NL_SERVER_IP and REALITY_PUBLIC_KEY:
        links.append({
            "name": f"🇳🇱 Европа (REALITY)",
            "link": build_vless_reality_link(key, NL_SERVER_IP, REALITY_PORT, f"NL-Reality-{key.name}"),
            "type": "vless-reality"
        })

    # RU сервер через REALITY
    if RU_SERVER_IP and REALITY_PUBLIC_KEY:
        links.append({
            "name": f"🇷🇺 Россия (REALITY)",
            "link": build_vless_reality_link(key, RU_SERVER_IP, REALITY_PORT, f"RU-Reality-{key.name}"),
            "type": "vless-reality"
        })

    # RU сервер через WS (если есть домен)
    if RU_SERVER_DOMAIN and RU_SERVER_DOMAIN != DOMAIN:
        links.append({
            "name": f"🇷🇺 Россия (WS+TLS)",
            "link": build_vless_ws_link(key, RU_SERVER_DOMAIN, VLESS_WS_PORT, f"RU-{key.name}"),
            "type": "vless-ws"
        })

    return links


def get_subscription_content(keys: list[VPNKey]) -> str:
    """Собрать base64-подписку из всех ключей пользователя.
    Подписка содержит только WS+TLS ссылки (безопасно для клиентов).
    """
    import base64
    all_links = []
    for key in keys:
        if key.status != "active":
            continue
        for link_info in get_user_links(key):
            if link_info["type"] == "vless-ws":  # только WS+TLS
                all_links.append(link_info["link"])
    return base64.b64encode("\n".join(all_links).encode()).decode()


def get_xray_stats(uuid: str) -> dict[str, int]:
    """Получить статистику трафика из Xray API для клиента по UUID."""
    try:
        # Используем xray api для получения статистики
        result = subprocess.run(
            ["xray", "api", "statsquery", "--server=127.0.0.1:10085",
             f"-pattern=user>>>{uuid}>>>"],
            capture_output=True, text=True, timeout=5
        )
        uplink = 0
        downlink = 0
        if result.returncode == 0:
            for line in result.stdout.split("\n"):
                if "uplink" in line.lower() and "value" in line.lower():
                    try:
                        uplink = int(line.split(":")[-1].strip())
                    except ValueError:
                        pass
                if "downlink" in line.lower() and "value" in line.lower():
                    try:
                        downlink = int(line.split(":")[-1].strip())
                    except ValueError:
                        pass
        return {"uplink": uplink, "downlink": downlink, "total": uplink + downlink}
    except Exception as e:
        logger.debug("Не удалось получить статистику Xray: %s", e)
        return {"uplink": 0, "downlink": 0, "total": 0}
