#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  09-deploy-xray-node.sh — Установка xray-node на remote сервер
# ═══════════════════════════════════════════════════════════════════════════
#
#  Устанавливает xray-node (API для управления remote Xray) на сервер.
#  Может быть запущен напрямую на сервере, или через SSH.
#
#  ИСПОЛЬЗОВАНИЕ:
#    # На сервере с уже установленным Xray (RU сервер):
#    bash 09-deploy-xray-node.sh 193.163.203.40 9090 --skip-xray-install
#
#    # На сервере без Xray (доп. NL, Finland):
#    bash 09-deploy-xray-node.sh 91.229.105.51 9090
#
#    # Через SSH:
#    ssh root@91.229.105.51 "bash -s" < scripts/09-deploy-xray-node.sh 91.229.105.51 9090
#
#  Запуск: bash scripts/09-deploy-xray-node.sh <SERVER_IP> <API_PORT> [OPTIONS]
#
#  OPTIONS:
#    --skip-xray-install   — Не устанавливать Xray core (уже есть)
#    --main-server IP      — IP основного сервера для UFW (по умолч. 66.248.207.111)
#    --token TOKEN         — XRAY_NODE_TOKEN (если не указан — спросит)
#    --repo-url URL        — URL git-репозитория
# ═══════════════════════════════════════════════════════════════════════════

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
sep()  { echo -e "${CYAN}──────────────────────────────────────────────${NC}"; }

# ── Аргументы ──
SERVER_IP="${1:-}"
API_PORT="${2:-9090}"
SKIP_XRAY=false
MAIN_SERVER_IP="66.248.207.111"
XRAY_NODE_TOKEN=""
REPO_URL=""

shift 2 || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-xray-install) SKIP_XRAY=true ;;
        --main-server) MAIN_SERVER_IP="$2"; shift ;;
        --token) XRAY_NODE_TOKEN="$2"; shift ;;
        --repo-url) REPO_URL="$2"; shift ;;
        *) warn "Неизвестный флаг: $1" ;;
    esac
    shift
done

if [ -z "$SERVER_IP" ]; then
    err "Укажите SERVER_IP: bash $0 <IP> [PORT]"
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    err "Запустите от root: sudo bash $0"
    exit 1
fi

# ── Запрос токена если не указан ──
if [ -z "$XRAY_NODE_TOKEN" ]; then
    echo -n "Введите XRAY_NODE_TOKEN (или нажмите Enter для генерации): "
    read -r XRAY_NODE_TOKEN
    if [ -z "$XRAY_NODE_TOKEN" ]; then
        XRAY_NODE_TOKEN=$(openssl rand -hex 32)
        ok "Сгенерирован токен: ${XRAY_NODE_TOKEN}"
        echo "  → Скопируйте этот токен в .env основного сервера: XRAY_NODE_TOKEN=${XRAY_NODE_TOKEN}"
    fi
fi

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Установка xray-node на ${SERVER_IP}              ${NC}"
echo -e "${CYAN}  API порт: ${API_PORT}                              ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
echo ""

# ── 1. Системные зависимости ──
log "Обновление системы..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl wget git python3 python3-pip python3-venv ufw openssl

# Убедимся что pip установлен
if ! command -v pip3 &> /dev/null; then
    apt-get install -y -qq python3-pip
fi

ok "Системные пакеты установлены"

# ── 2. Xray Core ──
if [ "$SKIP_XRAY" = true ]; then
    log "Пропускаем установку Xray core (--skip-xray-install)"
    # Проверим что Xray установлен
    if ! systemctl is-active --quiet xray 2>/dev/null && ! command -v xray &> /dev/null; then
        warn "Xray не найден в системе! Убедитесь что он установлен."
        warn "Если его нет — перезапустите скрипт без --skip-xray-install"
    else
        ok "Xray core уже установлен"
    fi
else
    log "Установка Xray core..."
    if ! command -v xray &> /dev/null; then
        bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
        ok "Xray core установлен"
    else
        ok "Xray core уже установлен"
    fi

    # Создаём директорию для конфигов
    mkdir -p /etc/xray
    # Если конфиг пустой — создаём минимальный
    if [ ! -f /etc/xray/config.json ] || [ ! -s /etc/xray/config.json ]; then
        log "Создаём минимальный Xray config..."
        cat > /etc/xray/config.json << 'EOF'
{
  "log": { "loglevel": "warning" },
  "inbounds": [],
  "outbounds": [{ "protocol": "freedom", "tag": "direct" }]
}
EOF
        ok "Минимальный Xray config создан в /etc/xray/config.json"
        warn "Он будет перезаписан основным сервером при первой синхронизации"
    fi

    # Запускаем Xray если не запущен
    if ! systemctl is-active --quiet xray 2>/dev/null; then
        systemctl enable xray
        systemctl start xray
        ok "Xray сервис запущен"
    fi
fi

# ── 3. Клонирование репозитория ──
PROJECT_DIR="/opt/ufobzk"
log "Клонирование репозитория..."
if [ -d "$PROJECT_DIR/.git" ]; then
    cd "$PROJECT_DIR"
    git pull origin main 2>/dev/null || git pull origin master 2>/dev/null || warn "Git pull не удался — продолжаем"
else
    if [ -n "$REPO_URL" ]; then
        git clone "$REPO_URL" "$PROJECT_DIR"
    else
        # Попробуем найти repo URL из git remote если запущено из клонированного репо
        CURRENT_REPO="$(git remote get-url origin 2>/dev/null || echo "")"
        if [ -n "$CURRENT_REPO" ]; then
            git clone "$CURRENT_REPO" "$PROJECT_DIR"
        else
            err "Не указан URL репозитория. Используйте --repo-url или запустите из git-репозитория."
            exit 1
        fi
    fi
fi
ok "Репозиторий готов"

# ── 4. Python зависимости ──
log "Установка Python-зависимостей..."
cd "$PROJECT_DIR/xray-node"
pip3 install -q -r requirements.txt
ok "Python-зависимости установлены"

# ── 5. Создание .env ──
log "Настройка .env для xray-node..."
cat > "$PROJECT_DIR/xray-node/.env" <<EOF
XRAY_NODE_TOKEN=${XRAY_NODE_TOKEN}
XRAY_NODE_PORT=${API_PORT}
XRAY_CONFIG_PATH=/etc/xray/config.json
XRAY_CONTAINER_NAME=xray
EOF
ok ".env создан: ${PROJECT_DIR}/xray-node/.env"

# ── 6. Systemd сервис ──
log "Создание systemd сервиса..."
cat > /etc/systemd/system/xray-node.service <<EOF
[Unit]
Description=UFOBZK Xray Node API
After=network.target xray.service
Wants=xray.service

[Service]
Type=simple
User=root
WorkingDirectory=${PROJECT_DIR}/xray-node
Environment=XRAY_NODE_TOKEN=${XRAY_NODE_TOKEN}
Environment=XRAY_NODE_PORT=${API_PORT}
Environment=XRAY_CONFIG_PATH=/etc/xray/config.json
Environment=XRAY_CONTAINER_NAME=xray
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port ${API_PORT}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable xray-node
systemctl restart xray-node

sleep 2

if systemctl is-active --quiet xray-node; then
    ok "xray-node сервис запущен и работает"
else
    err "xray-node не запустился!"
    systemctl status xray-node --no-pager
    exit 1
fi

# ── 7. UFW / Брандмауэр ──
log "Настройка брандмауэра..."

# Разрешаем SSH
ufw allow 22/tcp comment 'SSH' >/dev/null 2>&1 || true

# Разрешаем Xray VPN (443)
ufw allow 443/tcp comment 'Xray VPN' >/dev/null 2>&1 || true

# API xray-node — ТОЛЬКО с основного сервера
ufw delete allow ${API_PORT}/tcp >/dev/null 2>&1 || true  # удаляем старое общее правило
ufw allow from ${MAIN_SERVER_IP} to any port ${API_PORT} proto tcp comment "xray-node API only from main server" >/dev/null 2>&1 || true

# Включаем UFW если ещё не включён
ufw --force enable >/dev/null 2>&1 || true

ok "Брандмауэр настроен"
ufw status | grep -E "${API_PORT}|443|22" || true

# ── 8. Проверка ──
log "Проверка API..."
sleep 2

HEALTH=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer ${XRAY_NODE_TOKEN}" \
    "http://127.0.0.1:${API_PORT}/health" 2>/dev/null || echo "000")

if [ "$HEALTH" = "200" ]; then
    ok "API отвечает (200 OK)"
else
    warn "API вернул ${HEALTH} — проверьте: journalctl -u xray-node -n 50"
fi

# ── 9. Итог ──
echo ""
sep
echo ""
echo -e "  ${GREEN}${BOLD}xray-node установлен на ${SERVER_IP}!${NC}"
echo ""
echo -e "  ${BOLD}API:${NC} http://${SERVER_IP}:${API_PORT}"
echo -e "  ${BOLD}Token:${NC} ${XRAY_NODE_TOKEN}"
echo -e "  ${BOLD}Xray config:${NC} /etc/xray/config.json"
echo ""
echo -e "  ${BOLD}Команды:${NC}"
echo -e "  systemctl status xray-node   — статус"
echo -e "  journalctl -u xray-node -f   — логи в реальном времени"
echo -e "  systemctl restart xray-node — перезапуск"
echo ""
echo -e "  ${BOLD}Добавьте сервер в админку основного сервера:${NC}"
echo -e "  → https://ufolabs.online/admin → Servers"
echo -e "  → Host: ${SERVER_IP}"
echo -e "  → Port: ${API_PORT}"
echo -e "  → Token: ${XRAY_NODE_TOKEN}"
echo -e "  → Region: NL или FI (или другое)"
echo ""
