"""Удалённое управление Xray nodes — пуш конфига на remote серверы."""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import Server, VPNKey

logger = logging.getLogger(__name__)

# Таймауты для HTTP запросов
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# Каскад: NL/FI → RU для российского трафика
RU_SERVER_IP = os.getenv("RU_SERVER_IP", "")
RU_TRANSIT_UUID = os.getenv("RU_TRANSIT_UUID", "")
RU_TRANSIT_PORT = int(os.getenv("RU_TRANSIT_PORT", "443"))
RU_TRANSIT_PUBLIC_KEY = os.getenv("RU_TRANSIT_PUBLIC_KEY", "")
RU_TRANSIT_SHORT_ID = os.getenv("RU_TRANSIT_SHORT_ID", "aabbccdd")
RU_TRANSIT_SN = os.getenv("RU_TRANSIT_SN", "www.yandex.ru")


def _build_remote_config(server: Server, keys: list[VPNKey]) -> dict[str, Any]:
    """Собрать Xray config для remote сервера (только inbound + minimal outbound)."""
    valid_keys = [k for k in keys if k.status == "active"]

    vless_clients = []
    reality_clients = []
    for key in valid_keys:
        if key.protocol == "vless":
            vless_clients.append({
                "id": key.uuid,
                "email": key.uuid,
                "flow": "",
                "level": 0
            })
            reality_clients.append({
                "id": key.uuid,
                "email": key.uuid,
                "flow": "xtls-rprx-vision",
                "level": 0
            })

    inbounds: list[dict[str, Any]] = [
        {
            "tag": "VLESS-WS",
            "listen": "0.0.0.0",
            "port": server.ws_port,
            "protocol": "vless",
            "settings": {"clients": vless_clients, "decryption": "none"},
            "streamSettings": {
                "network": "ws",
                "security": "none",
                "wsSettings": {"path": "/vless-ws", "heartbeatPeriod": 30}
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}
        },
        {
            "tag": "VLESS-XHTTP",
            "listen": "0.0.0.0",
            "port": server.xhttp_port,
            "protocol": "vless",
            "settings": {"clients": vless_clients, "decryption": "none"},
            "streamSettings": {
                "network": "xhttp",
                "security": "none",
                "xhttpSettings": {"path": "/xhttp", "mode": "auto", "xPaddingBytes": "100-1000"}
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}
        },
        {
            "tag": "VLESS-gRPC",
            "listen": "0.0.0.0",
            "port": server.grpc_port,
            "protocol": "vless",
            "settings": {"clients": vless_clients, "decryption": "none"},
            "streamSettings": {
                "network": "grpc",
                "security": "none",
                "grpcSettings": {"serviceName": "VpnService", "multiMode": False}
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}
        },
    ]

    # REALITY inbound если настроен
    if server.reality_private_key and server.reality_short_id:
        inbounds.append({
            "tag": "VLESS-REALITY",
            "listen": "0.0.0.0",
            "port": server.reality_port,
            "protocol": "vless",
            "settings": {"clients": reality_clients, "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": server.reality_dest or "www.samsung.com:443",
                    "xver": 0,
                    "serverNames": [s.strip() for s in (server.reality_server_names or "www.samsung.com").split(",")],
                    "privateKey": server.reality_private_key,
                    "shortIds": [server.reality_short_id]
                }
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]}
        })

    config: dict[str, Any] = {
        "log": {"loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": [
            {"tag": "DIRECT", "protocol": "freedom", "settings": {"domainStrategy": "UseIP", "packetEncoding": "xudp"}},
            {"tag": "BLACKHOLE", "protocol": "blackhole"}
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {"type": "field", "outboundTag": "BLACKHOLE", "protocol": ["bittorrent"]},
                {"type": "field", "outboundTag": "DIRECT", "network": "tcp,udp"}
            ]
        }
    }

    # Каскад: российский трафик → RU сервер (выход с RU IP)
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


async def push_config_to_server(server: Server, keys: list[VPNKey]) -> bool:
    """Отправить конфиг на remote Xray сервер."""
    config = _build_remote_config(server, keys)
    headers = {"Authorization": f"Bearer {server.api_token}"}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=True) as client:
            resp = await client.post(
                f"{server.api_url.rstrip('/')}/config",
                json=config,
                headers=headers
            )
            resp.raise_for_status()
            data = resp.json()
            ok = data.get("ok", False)
            restarted = data.get("restarted", False)
            if ok and restarted:
                logger.info("Config pushed and restarted on %s", server.name)
                return True
            elif ok:
                logger.warning("Config pushed but restart failed on %s: %s", server.name, data.get("error"))
                return True  # config applied but restart manual
            else:
                logger.error("Server %s rejected config: %s", server.name, data)
                return False
    except httpx.HTTPStatusError as e:
        logger.error("HTTP error pushing to %s: %s %s", server.name, e.response.status_code, e.response.text[:200])
        return False
    except Exception as e:
        logger.error("Failed to push config to %s: %s", server.name, e)
        return False


async def sync_server(db: Session, server: Server) -> bool:
    """Синхронизировать конфиг на один remote сервер."""
    keys = db.query(VPNKey).filter(VPNKey.is_active == True).all()  # noqa: E712
    return await push_config_to_server(server, keys)


async def sync_all_servers(db: Session) -> dict[str, bool]:
    """Синхронизировать конфиг на все активные remote серверы."""
    servers = db.query(Server).filter(Server.is_active == True).all()  # noqa: E712
    if not servers:
        return {}

    keys = db.query(VPNKey).filter(VPNKey.is_active == True).all()  # noqa: E712
    results = {}

    for server in servers:
        success = await push_config_to_server(server, keys)
        server.last_sync = datetime.now(timezone.utc)
        server.last_sync_status = "ok" if success else "error"
        results[server.name] = success

    db.commit()
    return results
