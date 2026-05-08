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
                "email": key.uuid,
                "flow": "",
                "level": 0
            })

    # Клиенты для VLESS REALITY
    reality_clients = []
    for key in valid_keys:
        if key.protocol == "vless":
            reality_clients.append({
                "id": key.uuid,
                "email": key.uuid,
                "flow": "xtls-rprx-vision",
                "level": 0
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
                    "statsUserDownlink": True,
                    "handshake": 8,
                    "connIdle": 300,
                    "uplinkOnly": 10,
                    "downlinkOnly": 15
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
                        "mode": "auto",
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
    1. docker restart (production — xray в отдельном контейнере)
    2. systemctl restart xray (bare metal)
    3. killall -HUP xray (bare metal без systemd)
    """
    docker_sock = "/var/run/docker.sock"
    container_name = os.getenv("XRAY_CONTAINER_NAME", "ufobzk-xray")

    # ── Попытка 1: docker CLI (есть сокет → контейнер запущен в Docker) ──
    if os.path.exists(docker_sock):
        try:
            result = subprocess.run(
                ["docker", "restart", "-t", "5", container_name],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                logger.info("Xray перезагружен через docker restart")
                return True
            logger.warning("docker restart вернул код %s: %s", result.returncode, result.stderr.strip())
        except FileNotFoundError:
            logger.debug("docker CLI не найден")
        except Exception as e:
            logger.warning("Не удалось перезагрузить через docker restart: %s", e)

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
        f"&fp=chrome&alpn=http%2F1.1"
    )
    return f"vless://{key.uuid}@{server_domain}:{port}?{params}#{quote(remark)}"


def build_vless_xhttp_link(key: VPNKey, server_domain: str, port: int = 443, remark: str = "") -> str:
    """VLESS XHTTP + TLS ссылка (новый транспорт, лучше WS для DPI)."""
    if not remark:
        remark = f"{key.name}@{server_domain}-xhttp"
    params = (
        f"type=xhttp&security=tls&host={server_domain}"
        f"&path=%2Fxhttp&sni={server_domain}"
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


def _build_outbound_for_link(key: VPNKey, link_info: dict[str, Any]) -> dict[str, Any] | None:
    """Построить outbound JSON для одной ссылки."""
    server_host = link_info["link"].split("@")[1].split(":")[0] if "@" in link_info["link"] else DOMAIN
    port = VLESS_WS_PORT
    if link_info["type"] == "vless-ws":
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
