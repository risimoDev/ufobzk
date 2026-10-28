"""Управление Xray — генерация конфигов, VLESS-ссылок, перезагрузка ядра."""

import json
import logging
import os
import re as _re_mod
import subprocess
import threading
import uuid as _uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
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
# Что уводить в гео-выход. Синтаксис routing-правил Xray.
#
# connectivitycheck.gstatic.com, dns.google и mtalk.google.com (FCM) входят в
# geosite:google, но для внешнего выхода (нода/WARP) они отводятся в DIRECT
# отдельным правилом выше — см. _GEO_BYPASS_DOMAINS. Иначе при падении выхода
# Android решит, что интернета нет, и приложения встанут при живом туннеле.
#
# Antigravity: сайт на antigravity.google, служебные запросы IDE на *.codeium.com,
# вызовы модели — в Google Cloud AI Companion (*.googleapis.com). Без них IDE
# отвечает "User location is not supported for the API use".
#
# Список покрывает ВСЕ сервисы Google целиком. Раньше в гео-выход уходили только
# отдельные домены (www.google.com, gemini…), а youtube.com, accounts.google.com,
# play.google.com и прочие шли через IPv4 главного — и Google продолжал видеть RU.
# Регион Google выставляет по cookie/аккаунту, собранным на ЛЮБОМ своём домене,
# поэтому выпускать нужно всё семейство сразу.
_GEO_DOMAINS_DEFAULT = (
    "geosite:google,geosite:youtube,"
    # TLD .google: antigravity.google, gemini.google, blog.google …
    "domain:google,"
    "domain:google.com,domain:google.ru,domain:googleapis.com,domain:gstatic.com,"
    "domain:googleusercontent.com,domain:withgoogle.com,domain:gvt1.com,domain:gvt2.com,"
    "domain:youtube.com,domain:youtu.be,domain:googlevideo.com,domain:ytimg.com,domain:ggpht.com,"
    # Antigravity построен на Codeium/Windsurf — служебные запросы IDE идут сюда
    "domain:codeium.com,"
    "domain:nvidia.com,domain:ngc.nvidia.com,domain:nvcr.io"
)

# Домены, которые НЕ стоит уводить на внешний выход (нода/WARP): если он ляжет,
# Android по connectivitycheck решит, что интернета нет, а FCM-пуши встанут.
# Для GEO-V6 правило не нужно — это тот же сервер, отдельной точки отказа нет.
_GEO_BYPASS_DOMAINS = [
    "full:connectivitycheck.gstatic.com",
    "full:mtalk.google.com",
    "domain:dns.google",
]

# Домены Meta (Instagram, Facebook, Threads, CDN), Cloudflare и NVIDIA для защиты от попадания в RU-каскад
_META_DOMAINS = [
    "geosite:instagram",
    "geosite:facebook",
    "geosite:meta",
    "domain:instagram.com",
    "domain:cdninstagram.com",
    "domain:fbcdn.net",
    "domain:threads.net",
    "domain:cloudflare.com",
    "domain:nvidia.com",
    "domain:ngc.nvidia.com",
    "domain:nvcr.io",
]

# Отдельного списка для IPv6-выхода больше нет: GEO-V6 работает в режиме
# UseIPv6v4, и домен без AAAA уходит по IPv4, а не обрывается.
#
# GEO_DOMAINS ДОБАВЛЯЕТСЯ к встроенному списку, а не заменяет его: в старых .env
# лежат урезанные списки, и с ними Google снова видел бы RU. Старое имя
# WARP_DOMAINS намеренно не читается по той же причине.
GEO_DOMAINS = os.getenv("GEO_DOMAINS", "")

# Выпускать гео-домены через IPv6 главного сервера (GEO_IPV6=1) или через IPv4
# (GEO_IPV6=0, тогда Google идёт в DIRECT). Страну Google определяет поведенчески,
# и она плавает: до октября 2026 IPv4 был RU, а IPv6 — PL, 21.10.2026 они
# поменялись местами. ТРЕБУЕТ IPv6 на хосте и внутри docker-сети backend. Без
# него UseIPv6v4 молча уходит в IPv4. Проверка: check_ipv6_egress().
#
# GEO_IPV6=auto — семью выбирает фоновая задача (app/tasks.py: periodic_geo_auto):
# периодически спрашивает Google о стране по IPv4 и по IPv6 и переключает выход,
# когда текущий адрес попадает в GEO_AUTO_BAD_COUNTRIES. Выбор хранится в
# GEO_AUTO_STATE_PATH и переживает рестарт.
_GEO_IPV6_RAW = os.getenv("GEO_IPV6", "0").strip().lower()
GEO_IPV6_AUTO = _GEO_IPV6_RAW == "auto"
GEO_IPV6 = _GEO_IPV6_RAW in ("1", "true", "yes", "on")
GEO_AUTO_STATE_PATH = Path(os.getenv("GEO_AUTO_STATE_PATH", "data/geo_family"))
GEO_AUTO_BAD_COUNTRIES = {
    c.strip().upper() for c in os.getenv("GEO_AUTO_BAD_COUNTRIES", "RU,BY").split(",") if c.strip()
}

# Локальный SOCKS-вход для проверки маршрутизации С СЕРВЕРА. ipregion/curl в
# шелле хоста идут мимо Xray и всегда показывают «голый» IPv4 (RU у Google).
# Через этот порт запрос проходит те же правила, что и трафик клиентов:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Davoyan/ipregion/main/ipregion.sh) -p 127.0.0.1:10808
# Наружу порт опубликован только на 127.0.0.1 хоста (см. docker-compose.yml).
DIAG_SOCKS_PORT = 10808

# Своя нода как чистый выход для гео-чувствительных доменов — приоритетнее WARP.
# GEO_NODE_NAME — поле name строки в таблице servers (как в админке); host, порт
# и ключи REALITY берутся оттуда же, дублировать их в .env не нужно.
# GEO_TRANSIT_UUID должен попасть в клиенты REALITY-инбаунда этой ноды —
# см. app/remote_xray.py, иначе нода отобьёт подключение.
GEO_NODE_NAME = os.getenv("GEO_NODE_NAME", "")
GEO_TRANSIT_UUID = os.getenv("GEO_TRANSIT_UUID", "")

# Отдельный сервер-выход, которого нет в таблице servers (не нода для
# пользователей, голый xray — scripts/22-geo-exit-setup.sh печатает эти строки).
# Приоритетнее GEO_NODE_NAME. Включается, когда заданы HOST + UUID + PUBLIC_KEY.
GEO_EXIT_HOST = os.getenv("GEO_EXIT_HOST", "")
GEO_EXIT_PORT = int(os.getenv("GEO_EXIT_PORT", "443"))
GEO_EXIT_UUID = os.getenv("GEO_EXIT_UUID", "")
GEO_EXIT_PUBLIC_KEY = os.getenv("GEO_EXIT_PUBLIC_KEY", "")
GEO_EXIT_SHORT_ID = os.getenv("GEO_EXIT_SHORT_ID", "")
GEO_EXIT_SN = os.getenv("GEO_EXIT_SN", "www.samsung.com")
# GEO_EXIT_MODE=reverse — выход сам подключается к REALITY главного (VLESS
# reverse, Xray 25.9+): нужен, когда главный не может открыть TCP к выходу
# (так режет Hostkey ↔ наш хостер), а обратно — может. Тогда нужен только
# GEO_EXIT_UUID: клиент с reverse добавляется в VLESS-REALITY, а GEO-EXIT —
# это его обратный туннель. Выход ставится scripts/22-geo-exit-setup.sh reverse.
GEO_EXIT_REVERSE = os.getenv("GEO_EXIT_MODE", "").strip().lower() == "reverse"
GEO_EXIT_STATIC = bool(GEO_EXIT_UUID and (GEO_EXIT_REVERSE or (GEO_EXIT_HOST and GEO_EXIT_PUBLIC_KEY)))

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
    # order_by обязателен: write_xray_config сравнивает текст конфига, и без
    # стабильного порядка клиентов «изменение» находилось бы на ровном месте —
    # с рестартом Xray и обрывом всех соединений.
    active_keys = db.query(VPNKey).filter(
        VPNKey.is_active == True  # noqa: E712
    ).order_by(VPNKey.id).all()

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
    # Обратный туннель от сервера-выхода: этот UUID не ходит наружу как
    # пользователь, его соединения становятся outbound'ом GEO-EXIT.
    # Без vision: с ним туннель поднимается, но ответы обратно не идут.
    if GEO_EXIT_STATIC and GEO_EXIT_REVERSE:
        reality_clients.append({
            "id": GEO_EXIT_UUID,
            "flow": "",
            "reverse": {"tag": "GEO-EXIT"}
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
        "dns": {
            "servers": [
                "1.1.1.1",
                "8.8.8.8"
            ],
            "queryStrategy": "UseIP"
        },
        "outbounds": [
            {
                "tag": "DIRECT",
                "protocol": "freedom",
                "settings": {
                    "domainStrategy": "UseIPv4",
                    "packetEncoding": "xudp"
                }
            },
            {
                "tag": "BLACKHOLE",
                "protocol": "blackhole"
            }
        ],
        # Catch-all правила (tcp,udp → DIRECT) в конце НЕТ, и это важно. При
        # IPIfNonMatch Xray резолвит домен и проверяет geoip-правила только если
        # на первом проходе не сработало НИ ОДНО правило. Catch-all срабатывал
        # всегда, поэтому geoip:ru (каскад) и geoip:private для доменов были
        # мёртвыми. Несовпавший трафик и так уходит в первый outbound — DIRECT.
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
                    "outboundTag": "BLACKHOLE",
                    "network": "udp",
                    "port": "443"
                },
                # Клиенты не должны попадать во внутреннюю docker-сеть и на
                # localhost контейнера: там stats API Xray (10085), ufo-app:8000
                # (доверяет X-Forwarded-For от кого угодно) и DIAG-SOCKS.
                {
                    "type": "field",
                    "outboundTag": "BLACKHOLE",
                    "ip": ["geoip:private"]
                }
            ]
        }
    }

    config["inbounds"].append({
        "tag": "DIAG-SOCKS",
        "listen": "0.0.0.0",
        "port": DIAG_SOCKS_PORT,
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": False},
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls"]
        }
    })

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
                    "minClientVer": "1.0.0",
                    "maxClientVer": "",
                    "maxTimeDiff": 0,
                    "shortIds": [s.strip() for s in REALITY_SHORT_ID.split(",") if s.strip()] + [""] if REALITY_SHORT_ID else [""]
                }
            },
            "sniffing": {
                "enabled": True,
                "destOverride": ["http", "tls", "quic"]
            }
        })

    # ── Выход для гео-чувствительных доменов ──
    # Приоритет: своя нода (GEO-EXIT) → Cloudflare WARP. Нода надёжнее: WARP
    # раздаёт общие адреса Cloudflare и режет их для серверных диапазонов —
    # handshake проходит, а трафик молча выбрасывается.
    geo_outbound_tag = None
    # Если нода выбрана явно, отката на WARP быть не должно: при опечатке в
    # имени тихо уехать на другой выход хуже, чем остаться на DIRECT.
    geo_node_requested = bool(GEO_NODE_NAME and GEO_TRANSIT_UUID) and not GEO_EXIT_STATIC

    if GEO_EXIT_STATIC and GEO_EXIT_REVERSE:
        geo_outbound_tag = "GEO-EXIT"
    elif GEO_EXIT_STATIC:
        config["outbounds"].append({
            "tag": "GEO-EXIT",
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": GEO_EXIT_HOST,
                    "port": GEO_EXIT_PORT,
                    "users": [{
                        "id": GEO_EXIT_UUID,
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
                    "serverName": GEO_EXIT_SN,
                    "publicKey": GEO_EXIT_PUBLIC_KEY,
                    "shortId": GEO_EXIT_SHORT_ID
                }
            },
            "mux": {"enabled": False}
        })
        geo_outbound_tag = "GEO-EXIT"

    if geo_node_requested:
        geo_node = db.query(Server).filter(
            Server.name == GEO_NODE_NAME,
            Server.is_active == True  # noqa: E712
        ).first()

        if geo_node is None:
            logger.warning(
                "GEO_NODE_NAME=%r не найден среди активных серверов — GEO-EXIT выключен",
                GEO_NODE_NAME,
            )
        elif not (geo_node.reality_public_key and geo_node.reality_short_id):
            logger.warning(
                "У сервера %r не заполнены ключи REALITY — GEO-EXIT выключен", GEO_NODE_NAME
            )
        else:
            geo_sn = (geo_node.reality_server_names or "www.samsung.com").split(",")[0].strip()
            config["outbounds"].append({
                "tag": "GEO-EXIT",
                "protocol": "vless",
                "settings": {
                    "vnext": [{
                        "address": geo_node.host,
                        "port": geo_node.reality_port,
                        "users": [{
                            "id": GEO_TRANSIT_UUID,
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
                        "serverName": geo_sn,
                        "publicKey": geo_node.reality_public_key,
                        "shortId": geo_node.reality_short_id
                    }
                },
                "mux": {"enabled": False}
            })
            geo_outbound_tag = "GEO-EXIT"
            logger.info("GEO-EXIT: гео-домены идут через ноду %s (%s)",
                        geo_node.name, geo_node.host)

    # IPv6 главного сервера. Никаких внешних зависимостей: тот же сервер, но
    # Google метит как RU только его IPv4, а IPv6 видит как PL.
    geo_v6 = GEO_IPV6 or (GEO_IPV6_AUTO and get_geo_family() == "v6")
    if geo_outbound_tag is None and not geo_node_requested and geo_v6:
        config["outbounds"].append({
            "tag": "GEO-V6",
            "protocol": "freedom",
            "settings": {"domainStrategy": "UseIPv6v4"}
        })
        geo_outbound_tag = "GEO-V6"

    # В режиме auto семья «v4» означает именно IPv4 главного (DIRECT), не WARP.
    if (geo_outbound_tag is None and not geo_node_requested and not GEO_IPV6
            and not GEO_IPV6_AUTO and WARP_PRIVATE_KEY and WARP_PUBLIC_KEY and WARP_ADDRESS_V4):
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
        geo_outbound_tag = "WARP"

    # ВАЖНО: правила для Google/YouTube и Meta стоят ВЫШЕ правил каскада.
    # 1. Google отдает для *.googlevideo.com адреса GGC-кэшей внутри РФ, которые матчатся на "geoip:ru".
    #    Без опережающего правила YouTube уходил бы в RU-хаб, где он заблокирован/замедлен РКН!
    #    Так же google.ru иначе поймался бы регэкспом \.ru$ и ушёл в RU-хаб.
    # 2. Instagram/Meta заблокированы в РФ, поэтому их трафик ВСЕГДА идёт через европейский выход.
    geo_domains: list[str] = []
    for d in (_GEO_DOMAINS_DEFAULT + "," + GEO_DOMAINS).split(","):
        d = d.strip()
        if d and d not in geo_domains:
            geo_domains.append(d)

    rules = config["routing"]["rules"]

    if geo_outbound_tag in ("GEO-EXIT", "WARP"):
        rules.append({
            "type": "field",
            "outboundTag": "DIRECT",
            "domain": _GEO_BYPASS_DOMAINS
        })

    # Google / YouTube — в гео-выход, если он настроен, иначе в DIRECT
    rules.append({
        "type": "field",
        "outboundTag": geo_outbound_tag or "DIRECT",
        "domain": geo_domains
    })

    # Правило для Meta / Instagram (всегда в DIRECT)
    rules.append({
        "type": "field",
        "outboundTag": "DIRECT",
        "domain": _META_DOMAINS
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

    return config


def check_ipv6_egress(timeout: float = 3.0) -> tuple[bool, str]:
    """Есть ли выход в интернет по IPv6 из docker-сети backend.

    Проверяется из контейнера приложения: он в той же сети backend, что и xray,
    так что результат совпадает с тем, что увидит GEO-V6. TCP-коннект к
    публичному DNS Google по IPv6 (порт 443 — у dns.google там DoH).
    """
    import socket
    try:
        sock = socket.create_connection(("2001:4860:4860::8888", 443), timeout=timeout)
        sock.close()
        return True, ""
    except OSError as e:
        return False, str(e)


def get_geo_family() -> str:
    """Через какую семью адресов сейчас выпускается Google в режиме GEO_IPV6=auto."""
    try:
        family = GEO_AUTO_STATE_PATH.read_text().strip()
    except OSError:
        family = ""
    return family if family in ("v4", "v6") else "v4"


def set_geo_family(family: str) -> None:
    GEO_AUTO_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    GEO_AUTO_STATE_PATH.write_text(family)


# Страна, которую Google проставляет на главной (hl_GL, например "ru_RU").
# Тот же разбор, что в scripts/17-diagnose-geo.sh и ipregion.sh: последнее
# совпадение в первой строке, где оно есть.
_GOOGLE_COUNTRY_RE = _re_mod.compile(r'"[a-z]{2}_([A-Z]{2})"')
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)


def detect_google_country(family: str, timeout: float = 15.0) -> str | None:
    """Какую страну Google видит у главного сервера по IPv4 ("v4") или IPv6 ("v6").

    Запрос идёт из контейнера приложения напрямую, мимо Xray: он в той же сети
    backend и выходит наружу с теми же адресами хоста, что и Xray. Без cookie —
    Google решает только по адресу. None — если проверить не удалось.
    """
    local_address = "0.0.0.0" if family == "v4" else "::"
    try:
        with httpx.Client(
            transport=httpx.HTTPTransport(local_address=local_address),
            timeout=timeout,
            headers={"User-Agent": _BROWSER_UA},
        ) as client:
            resp = client.get("https://www.google.com/")
    except httpx.HTTPError as e:
        logger.debug("Google по %s не ответил: %s", family, e)
        return None
    for line in resp.text.splitlines():
        found = _GOOGLE_COUNTRY_RE.findall(line)
        if found:
            return found[-1]
    return None


def log_geo_exit_status() -> None:
    """Громко предупредить при старте, если GEO-V6 выбран, но IPv6 не работает.

    UseIPv6v4 при отсутствии IPv6 молча уходит в IPv4 — снаружи это выглядит
    как «всё работает, но Google показывает RU».
    """
    if GEO_EXIT_STATIC and GEO_EXIT_REVERSE:
        logger.info("Гео-выход для Google/YouTube: обратный туннель от сервера-выхода (GEO-EXIT)")
        return
    if GEO_EXIT_STATIC:
        logger.info("Гео-выход для Google/YouTube: сервер %s:%s (GEO-EXIT)", GEO_EXIT_HOST, GEO_EXIT_PORT)
        return
    if GEO_NODE_NAME and GEO_TRANSIT_UUID:
        logger.info("Гео-выход для Google/YouTube: нода %s (GEO-EXIT)", GEO_NODE_NAME)
        return
    if GEO_IPV6_AUTO:
        logger.info("GEO-AUTO: семья выбирается автоматически, сейчас %s", get_geo_family())
        return
    if not GEO_IPV6:
        if not (WARP_PRIVATE_KEY and WARP_PUBLIC_KEY and WARP_ADDRESS_V4):
            logger.info(
                "Google/YouTube идут через IPv4 главного. Если Google видит его "
                "как RU — GEO_IPV6=1 или GEO_IPV6=auto (scripts/17-diagnose-geo.sh)."
            )
        return
    ok, err = check_ipv6_egress()
    if ok:
        logger.info("GEO-V6: выход по IPv6 из сети backend работает")
    else:
        logger.error(
            "GEO-V6 включён, но выхода по IPv6 нет (%s). Google/YouTube уходят "
            "по IPv4 и видят RU. Проверьте IPv6 на хосте (ip -6 addr show scope "
            "global) и enable_ipv6 у сети backend: scripts/17-diagnose-geo.sh",
            err,
        )


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


_sync_lock = threading.Lock()


def sync_and_reload(db: Session) -> bool:
    """Пересобрать конфиг и перезагрузить Xray (только если конфиг изменился).

    Вызывается из нескольких потоков сразу (фоновые задачи админки, бот,
    проверка истечения). Без блокировки два вызова одновременно пишут конфиг и
    дважды рестартят контейнер — клиенты рвутся два раза подряд.
    """
    with _sync_lock:
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
        {"type": "field", "outboundTag": "BLOCK", "network": "udp", "port": "443"},
    ]

    # YouTube, Google и Meta (Instagram) ВСЕГДА идут через EU-ноды (защита от попадания в RU)
    if eu_tags:
        safe_domains = [
            "geosite:youtube",
            "geosite:google",
            "geosite:instagram",
            "geosite:facebook",
            "geosite:meta",
            "domain:googlevideo.com",
            "domain:ytimg.com",
            "domain:youtube.com",
            "domain:youtu.be",
            "domain:youtubei.googleapis.com",
            "domain:gvt1.com",
            "domain:instagram.com",
            "domain:cdninstagram.com",
            "domain:fbcdn.net",
            "domain:threads.net",
            "domain:cloudflare.com",
            "domain:nvidia.com",
            "domain:ngc.nvidia.com",
            "domain:nvcr.io",
            "geosite:openai",
        ]
        routing_rules.append({
            "type": "field",
            "outboundTag": eu_tags[0],
            "domain": safe_domains,
        })

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
        "dns": {
            "servers": ["1.1.1.1", "8.8.8.8"],
            "queryStrategy": "UseIPv4"
        },
        "outbounds": outbounds + [
            {"tag": "DIRECT", "protocol": "freedom", "settings": {"domainStrategy": "UseIPv4", "packetEncoding": "xudp"}},
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
        # Заменяем правила для EU на balancer
        for rule in result["routing"]["rules"]:
            if rule.get("outboundTag") == eu_tags[0]:
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
