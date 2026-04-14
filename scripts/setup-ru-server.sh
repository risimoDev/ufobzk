#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  setup-ru-server.sh — Установка Xray на RU-сервер (Россия)
# ═══════════════════════════════════════════════════════════
#  RU-сервер — точка входа каскада:
#   - .ru / .su / .рф / geoip:ru → DIRECT (напрямую)
#   - Всё остальное → каскад через NL-сервер
#
#  Перед запуском вам нужны данные от NL-сервера:
#   - NL_SERVER_IP
#   - NL_REALITY_PUBLIC_KEY
#   - NL_REALITY_SHORT_ID
#   - TRANSIT_UUID
#
#  Запуск: sudo bash scripts/setup-ru-server.sh
# ═══════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${CYAN}[•]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
    err "Запустите от root: sudo bash $0"
    exit 1
fi

echo ""
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo -e "${CYAN}  Установка Xray — RU-сервер (Россия)     ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo ""

# ══════════════════════════════════════════
# 1. Входные данные от NL-сервера
# ══════════════════════════════════════════

echo -e "${YELLOW}Введите данные от NL-сервера (из вывода setup-nl-server.sh):${NC}"
echo ""

read -rp "NL-сервер IP: " NL_SERVER_IP
[ -n "$NL_SERVER_IP" ] || { err "IP не указан"; exit 1; }

read -rp "NL REALITY Public Key: " NL_PUBLIC_KEY
[ -n "$NL_PUBLIC_KEY" ] || { err "Public Key не указан"; exit 1; }

read -rp "NL REALITY Short ID: " NL_SHORT_ID
[ -n "$NL_SHORT_ID" ] || { err "Short ID не указан"; exit 1; }

read -rp "Transit UUID: " TRANSIT_UUID
[ -n "$TRANSIT_UUID" ] || { err "Transit UUID не указан"; exit 1; }

echo ""

# ══════════════════════════════════════════
# 2. Установка Xray
# ══════════════════════════════════════════

if command -v xray &>/dev/null; then
    ok "Xray уже установлен: $(xray version | head -1)"
else
    log "Установка Xray..."
    bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
    ok "Xray установлен: $(xray version | head -1)"
fi

# ══════════════════════════════════════════
# 3. Генерация REALITY ключей для RU-сервера
# ══════════════════════════════════════════

log "Генерация ключей x25519 для REALITY (RU-сервер)..."
KEY_OUTPUT=$(xray x25519 2>/dev/null)

RU_PRIVATE_KEY=$(echo "$KEY_OUTPUT" | grep -i "private" | awk '{print $NF}')
RU_PUBLIC_KEY=$(echo "$KEY_OUTPUT" | grep -i "public" | awk '{print $NF}')
RU_SHORT_ID=$(openssl rand -hex 8)

[ -n "$RU_PRIVATE_KEY" ] || { err "Не удалось сгенерировать Private Key"; exit 1; }
ok "Ключи REALITY сгенерированы для RU-сервера"

# ══════════════════════════════════════════
# 4. Создание конфигурации Xray
# ══════════════════════════════════════════

log "Создание конфигурации /etc/xray/config.json..."

mkdir -p /etc/xray /var/log/xray

cat > /etc/xray/config.json <<XRAYEOF
{
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
        "statsUserUplink": true,
        "statsUserDownlink": true
      }
    },
    "system": {
      "statsInboundUplink": true,
      "statsInboundDownlink": true,
      "statsOutboundUplink": true,
      "statsOutboundDownlink": true
    }
  },
  "inbounds": [
    {
      "tag": "api-inbound",
      "listen": "127.0.0.1",
      "port": 10085,
      "protocol": "dokodemo-door",
      "settings": {
        "address": "127.0.0.1"
      }
    },
    {
      "tag": "VLESS-WS",
      "listen": "0.0.0.0",
      "port": 8443,
      "protocol": "vless",
      "settings": {
        "clients": [],
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
        "enabled": true,
        "destOverride": ["http", "tls", "quic"]
      }
    },
    {
      "tag": "VLESS-REALITY",
      "listen": "0.0.0.0",
      "port": 443,
      "protocol": "vless",
      "settings": {
        "clients": [],
        "decryption": "none"
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "www.samsung.com:443",
          "xver": 0,
          "serverNames": ["www.samsung.com", "samsung.com"],
          "privateKey": "${RU_PRIVATE_KEY}",
          "shortIds": ["${RU_SHORT_ID}"]
        }
      },
      "sniffing": {
        "enabled": true,
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
      "tag": "NL-PROXY",
      "protocol": "vless",
      "settings": {
        "vnext": [
          {
            "address": "${NL_SERVER_IP}",
            "port": 443,
            "users": [
              {
                "id": "${TRANSIT_UUID}",
                "encryption": "none",
                "flow": "xtls-rprx-vision"
              }
            ]
          }
        ]
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "fingerprint": "chrome",
          "serverName": "www.samsung.com",
          "publicKey": "${NL_PUBLIC_KEY}",
          "shortId": "${NL_SHORT_ID}"
        }
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
        "outboundTag": "BLACKHOLE",
        "ip": ["geoip:private"]
      },
      {
        "type": "field",
        "outboundTag": "DIRECT",
        "domain": [
          "regexp:\\\\.ru$",
          "regexp:\\\\.su$",
          "regexp:\\\\.рф$",
          "domain:yandex.com",
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
        "outboundTag": "DIRECT",
        "ip": ["geoip:ru"]
      },
      {
        "type": "field",
        "outboundTag": "NL-PROXY",
        "network": "tcp,udp"
      }
    ]
  }
}
XRAYEOF

ok "Конфигурация Xray создана (каскад → NL-сервер)"

# ══════════════════════════════════════════
# 5. Фаервол
# ══════════════════════════════════════════

log "Настройка UFW..."
ufw allow 22/tcp     comment "SSH"          2>/dev/null || true
ufw allow 80/tcp     comment "HTTP"         2>/dev/null || true
ufw allow 443/tcp    comment "REALITY"      2>/dev/null || true
ufw allow 8443/tcp   comment "VLESS-WS"    2>/dev/null || true
echo "y" | ufw enable 2>/dev/null || true
ok "Фаервол настроен"

# ══════════════════════════════════════════
# 6. Запуск Xray
# ══════════════════════════════════════════

log "Запуск Xray..."
systemctl enable xray
systemctl restart xray

if systemctl is-active --quiet xray; then
    ok "Xray запущен и работает"
else
    err "Xray не запустился. Проверьте: journalctl -u xray -n 50"
    exit 1
fi

# ══════════════════════════════════════════
# 7. Проверка каскада
# ══════════════════════════════════════════

log "Проверка подключения к NL-серверу..."
if timeout 5 bash -c "echo >/dev/tcp/${NL_SERVER_IP}/443" 2>/dev/null; then
    ok "NL-сервер доступен на порту 443"
else
    warn "Не удалось подключиться к NL-серверу ${NL_SERVER_IP}:443"
    warn "Убедитесь, что NL-сервер запущен и порт 443 открыт"
fi

# ══════════════════════════════════════════
# Итоги
# ══════════════════════════════════════════

RU_IP=$(curl -4 -s ifconfig.me 2>/dev/null || echo "НЕ ОПРЕДЕЛЁН")

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  RU-сервер установлен!                                    ${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}IP адрес:${NC}              ${RU_IP}"
echo -e "  ${BOLD}RU REALITY Private:${NC}    ${RU_PRIVATE_KEY}"
echo -e "  ${BOLD}RU REALITY Public:${NC}     ${RU_PUBLIC_KEY}"
echo -e "  ${BOLD}RU Short ID:${NC}           ${RU_SHORT_ID}"
echo -e "  ${BOLD}Каскад через:${NC}          ${NL_SERVER_IP} (NL)"
echo ""
echo -e "  ${YELLOW}Маршрутизация:${NC}"
echo -e "    .ru / .su / .рф / geoip:ru  →  DIRECT (напрямую)"
echo -e "    Всё остальное               →  NL-сервер (каскад)"
echo ""
echo -e "  ${YELLOW}Для .env на основном сервере (если он на NL):${NC}"
echo -e "    RU_SERVER_IP=${RU_IP}"
echo ""
echo -e "  Конфиг: /etc/xray/config.json"
echo -e "  Логи:   /var/log/xray/"
echo -e "  Статус: systemctl status xray"
echo ""
echo -e "  ${YELLOW}Примечание:${NC} Клиенты (UUID) будут добавлены автоматически"
echo -e "  при создании ключей в админ-панели на основном сервере."
echo -e "  Для этого основной сервер перезапишет конфиг через sync."
echo ""
