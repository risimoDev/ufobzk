#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  setup-nl-server.sh — Установка Xray на NL-сервер (Нидерланды)
# ═══════════════════════════════════════════════════════════
#  NL-сервер — конечная точка каскада. Весь трафик уходит в интернет
#  напрямую. Принимает подключения:
#   - VLESS-WS (порт 8443) — через CDN/Nginx
#   - VLESS-REALITY (порт 443) — прямые подключения
#
#  Запуск: sudo bash scripts/setup-nl-server.sh
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
echo -e "${CYAN}  Установка Xray — NL-сервер (Нидерланды) ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo ""

# ══════════════════════════════════════════
# 1. Установка Xray
# ══════════════════════════════════════════

if command -v xray &>/dev/null; then
    ok "Xray уже установлен: $(xray version | head -1)"
else
    log "Установка Xray..."
    bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
    ok "Xray установлен: $(xray version | head -1)"
fi

# ══════════════════════════════════════════
# 2. Генерация REALITY ключей
# ══════════════════════════════════════════

log "Генерация ключей x25519 для REALITY..."
KEY_OUTPUT=$(xray x25519 2>/dev/null)

PRIVATE_KEY=$(echo "$KEY_OUTPUT" | grep -i "private" | awk '{print $NF}')
PUBLIC_KEY=$(echo "$KEY_OUTPUT" | grep -i "public" | awk '{print $NF}')

[ -n "$PRIVATE_KEY" ] || { err "Не удалось сгенерировать Private Key"; exit 1; }
[ -n "$PUBLIC_KEY" ]  || { err "Не удалось сгенерировать Public Key"; exit 1; }

# Генерация Short ID
SHORT_ID=$(openssl rand -hex 8)

ok "Ключи REALITY сгенерированы"

# ══════════════════════════════════════════
# 3. Запросить транзитный UUID
# ══════════════════════════════════════════

echo ""
echo -e "${YELLOW}Создание транзитного клиента для каскада.${NC}"
echo -e "Этот UUID будет использоваться RU-сервером для подключения к NL."
echo ""

TRANSIT_UUID=$(xray uuid)
ok "Транзитный UUID сгенерирован: ${TRANSIT_UUID}"

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
        "clients": [
          {"id": "${TRANSIT_UUID}", "email": "transit@cascade"}
        ],
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
        "clients": [
          {"id": "${TRANSIT_UUID}", "flow": "xtls-rprx-vision", "email": "transit@cascade"}
        ],
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
          "privateKey": "${PRIVATE_KEY}",
          "shortIds": ["${SHORT_ID}"]
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
        "network": "tcp,udp"
      }
    ]
  }
}
XRAYEOF

ok "Конфигурация Xray создана"

# ══════════════════════════════════════════
# 5. Фаервол
# ══════════════════════════════════════════

log "Настройка UFW..."
ufw allow 22/tcp     comment "SSH"         2>/dev/null || true
ufw allow 80/tcp     comment "HTTP"        2>/dev/null || true
ufw allow 443/tcp    comment "HTTPS/REALITY" 2>/dev/null || true
ufw allow 8443/tcp   comment "VLESS-WS"   2>/dev/null || true
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
# Итоги
# ══════════════════════════════════════════

NL_IP=$(curl -4 -s ifconfig.me 2>/dev/null || echo "НЕ ОПРЕДЕЛЁН")

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  NL-сервер установлен!                                    ${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}IP адрес:${NC}         ${NL_IP}"
echo -e "  ${BOLD}REALITY Private:${NC}  ${PRIVATE_KEY}"
echo -e "  ${BOLD}REALITY Public:${NC}   ${PUBLIC_KEY}"
echo -e "  ${BOLD}Short ID:${NC}         ${SHORT_ID}"
echo -e "  ${BOLD}Transit UUID:${NC}     ${TRANSIT_UUID}"
echo ""
echo -e "  ${YELLOW}Сохраните эти данные! Они нужны для настройки RU-сервера и .env${NC}"
echo ""
echo -e "  Для .env на основном сервере добавьте:"
echo -e "    NL_SERVER_IP=${NL_IP}"
echo -e "    REALITY_PUBLIC_KEY=${PUBLIC_KEY}"
echo -e "    REALITY_PRIVATE_KEY=${PRIVATE_KEY}"
echo -e "    REALITY_SHORT_ID=${SHORT_ID}"
echo ""
echo -e "  Конфиг: /etc/xray/config.json"
echo -e "  Логи:   /var/log/xray/"
echo -e "  Статус: systemctl status xray"
echo ""
