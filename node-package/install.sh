#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  UFOBZK VPN Node — установка REALITY-узла в ОДНУ команду
# ═══════════════════════════════════════════════════════════════════════════
#
#  Универсальный установщик: разворачивает на ЛЮБОМ чистом Ubuntu/Debian
#  сервере REALITY-узел (xray + node-api в Docker), который управляется
#  с основного сервера. Не требует домена, сертификатов и nginx.
#
#  БЫСТРЫЙ СТАРТ (на новом сервере, root):
#    git clone https://github.com/risimoDev/ufobzk.git /opt/ufobzk
#    cd /opt/ufobzk/node-package
#    bash install.sh --main-ip <IP_ОСНОВНОГО_СЕРВЕРА> --name "Finland-1" --region fi
#
#  ОПЦИИ:
#    --main-ip IP       IP основного сервера (для firewall; ОБЯЗАТЕЛЬНО)
#    --name NAME        Имя узла для админки (по умолч. hostname)
#    --region CODE      Регион: fi/nl/de/... (по умолч. eu)
#    --api-port PORT    Порт node-api (по умолч. 9090)
#    --reality-port P   Порт REALITY (по умолч. 443 — выглядит как HTTPS)
#    --sni HOST         Маскировочный SNI/dest (по умолч. www.samsung.com)
#    --token TOKEN      XRAY_NODE_TOKEN (по умолч. сгенерируется)
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${CYAN}[•]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── Аргументы ──
MAIN_IP=""; NODE_NAME="$(hostname)"; REGION="eu"; API_PORT="9090"
REALITY_PORT="443"; SNI="www.samsung.com"; TOKEN=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --main-ip)      MAIN_IP="$2"; shift 2 ;;
        --name)         NODE_NAME="$2"; shift 2 ;;
        --region)       REGION="$2"; shift 2 ;;
        --api-port)     API_PORT="$2"; shift 2 ;;
        --reality-port) REALITY_PORT="$2"; shift 2 ;;
        --sni)          SNI="$2"; shift 2 ;;
        --token)        TOKEN="$2"; shift 2 ;;
        *) err "Неизвестный флаг: $1"; exit 1 ;;
    esac
done

[ "$(id -u)" -eq 0 ] || { err "Запустите от root"; exit 1; }
[ -n "$MAIN_IP" ] || { err "Укажите --main-ip <IP основного сервера>"; exit 1; }

# Публичный IP этого узла (для админки)
NODE_IP="$(curl -s4 --max-time 5 ifconfig.me || curl -s4 --max-time 5 icanhazip.com || echo "")"

echo ""
echo -e "${CYAN}════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Установка UFOBZK VPN Node (REALITY)${NC}"
echo -e "${CYAN}  Узел: ${NODE_NAME} (${REGION})  IP: ${NODE_IP:-?}${NC}"
echo -e "${CYAN}════════════════════════════════════════════${NC}"
echo ""

# ── 1. Docker ──
if ! command -v docker &>/dev/null; then
    log "Установка Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    ok "Docker установлен"
else
    ok "Docker уже установлен"
fi

# docker compose v2 / v1
if docker compose version &>/dev/null; then DC="docker compose"
elif command -v docker-compose &>/dev/null; then DC="docker-compose"
else
    log "Установка docker compose plugin..."
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin
    DC="docker compose"
fi

# ── 2. Генерация REALITY ключей + токена ──
log "Генерация REALITY ключей (x25519)..."
KEY_OUT="$(docker run --rm teddysun/xray:latest xray x25519 2>/dev/null)"
PRIVATE_KEY="$(echo "$KEY_OUT" | grep -iE 'private' | awk '{print $NF}')"
PUBLIC_KEY="$(echo "$KEY_OUT"  | grep -iE 'public'  | awk '{print $NF}')"
[ -n "$PRIVATE_KEY" ] && [ -n "$PUBLIC_KEY" ] || { err "Не удалось сгенерировать ключи x25519"; echo "$KEY_OUT"; exit 1; }
SHORT_ID="$(openssl rand -hex 8)"
[ -n "$TOKEN" ] || TOKEN="$(openssl rand -hex 32)"
# Корневой домен SNI для serverNames (www.samsung.com → samsung.com)
ROOT_SNI="$(echo "$SNI" | sed -E 's/^www\.//')"
ok "Ключи сгенерированы (short_id=$SHORT_ID)"

# ── 3. .env ──
cat > .env <<EOF
XRAY_NODE_TOKEN=${TOKEN}
NODE_API_PORT=${API_PORT}
EOF
ok ".env создан"

# ── 4. Seed config (REALITY inbound, clients пустые — заполнит основной сервер) ──
mkdir -p config
cat > config/config.json <<EOF
{
  "log": { "loglevel": "warning" },
  "inbounds": [
    {
      "tag": "VLESS-REALITY",
      "listen": "0.0.0.0",
      "port": ${REALITY_PORT},
      "protocol": "vless",
      "settings": { "clients": [], "decryption": "none" },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "show": false,
          "dest": "${SNI}:443",
          "xver": 0,
          "serverNames": ["${SNI}", "${ROOT_SNI}"],
          "privateKey": "${PRIVATE_KEY}",
          "shortIds": ["${SHORT_ID}"]
        }
      },
      "sniffing": { "enabled": true, "destOverride": ["http", "tls", "quic"] }
    }
  ],
  "outbounds": [
    { "tag": "DIRECT", "protocol": "freedom", "settings": { "domainStrategy": "UseIP" } },
    { "tag": "BLACKHOLE", "protocol": "blackhole" }
  ],
  "routing": {
    "domainStrategy": "IPIfNonMatch",
    "rules": [ { "type": "field", "outboundTag": "BLACKHOLE", "protocol": ["bittorrent"] } ]
  }
}
EOF
ok "Seed-конфиг создан (REALITY на :${REALITY_PORT})"

# ── 5. Запуск ──
log "Сборка и запуск контейнеров..."
$DC up -d --build
ok "Контейнеры запущены"

# ── 6. Firewall (UFW) ──
if command -v ufw &>/dev/null || apt-get install -y -qq ufw; then
    ufw allow 22/tcp comment 'SSH' >/dev/null 2>&1 || true
    ufw allow ${REALITY_PORT}/tcp comment 'REALITY' >/dev/null 2>&1 || true
    # node-api — ТОЛЬКО с основного сервера
    ufw allow from ${MAIN_IP} to any port ${API_PORT} proto tcp comment 'node-api (main only)' >/dev/null 2>&1 || true
    ufw --force enable >/dev/null 2>&1 || true
    ok "Firewall настроен (REALITY:${REALITY_PORT} открыт, API:${API_PORT} только с ${MAIN_IP})"
fi

# ── 7. Проверка ──
sleep 4
HEALTH="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${API_PORT}/health" 2>/dev/null || echo 000)"
[ "$HEALTH" = "200" ] && ok "node-api отвечает (200)" || warn "node-api вернул ${HEALTH} — логи: $DC logs node-api"

# ── 8. Готовый блок для админки ──
echo ""
echo -e "${GREEN}${BOLD}═══ Узел установлен! Добавьте сервер в админку ═══${NC}"
echo -e "  ${BOLD}→ https://<ваш-домен>/admin → Servers → Add${NC}"
echo ""
echo -e "  Name:                 ${BOLD}${NODE_NAME}${NC}"
echo -e "  Host:                 ${BOLD}${NODE_IP}${NC}"
echo -e "  API URL:              ${BOLD}http://${NODE_IP}:${API_PORT}${NC}"
echo -e "  API Token:            ${BOLD}${TOKEN}${NC}"
echo -e "  Region:               ${BOLD}${REGION}${NC}"
echo -e "  Reality Port:         ${BOLD}${REALITY_PORT}${NC}"
echo -e "  Reality Public Key:   ${BOLD}${PUBLIC_KEY}${NC}"
echo -e "  Reality Private Key:  ${BOLD}${PRIVATE_KEY}${NC}"
echo -e "  Reality Short ID:     ${BOLD}${SHORT_ID}${NC}"
echo -e "  Reality Server Names: ${BOLD}${SNI},${ROOT_SNI}${NC}"
echo -e "  Reality Dest:         ${BOLD}${SNI}:443${NC}"
echo ""
echo -e "  ${YELLOW}После добавления основной сервер за ≤5 мин запушит ключи —${NC}"
echo -e "  ${YELLOW}узел появится в подписках пользователей автоматически.${NC}"
echo ""
echo -e "  Управление:  ${DC} ps | logs -f | restart   (в ${SCRIPT_DIR})"
echo ""

# Сохраняем креды в файл (на случай если потеряется вывод)
cat > node-credentials.txt <<EOF
UFOBZK VPN Node — ${NODE_NAME}
Host:                 ${NODE_IP}
API URL:              http://${NODE_IP}:${API_PORT}
API Token:            ${TOKEN}
Region:               ${REGION}
Reality Port:         ${REALITY_PORT}
Reality Public Key:   ${PUBLIC_KEY}
Reality Private Key:  ${PRIVATE_KEY}
Reality Short ID:     ${SHORT_ID}
Reality Server Names: ${SNI},${ROOT_SNI}
Reality Dest:         ${SNI}:443
EOF
chmod 600 node-credentials.txt
ok "Креды сохранены в ${SCRIPT_DIR}/node-credentials.txt (chmod 600)"
