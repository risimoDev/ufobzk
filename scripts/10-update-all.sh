#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  10-update-all.sh — Массовое обновление всех серверов
# ═══════════════════════════════════════════════════════════════════════════
#
#  Запускает обновление основного сервера и всех xray-node через SSH.
#
#  Запуск: sudo bash scripts/10-update-all.sh
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
sep()  { echo -e "${CYAN}══════════════════════════════════════════════${NC}"; }

if [ "$(id -u)" -ne 0 ]; then
    err "Запустите от root: sudo bash $0"
    exit 1
fi

PROJECT_DIR="${PROJECT_DIR:-/opt/ufobzk}"

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Массовое обновление всех серверов                        ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
echo ""

# ── 1. Основной сервер ──
sep
log "Шаг 1/4: Обновление основного сервера (66.248.207.111)..."
sep
echo ""

if [ -f "${PROJECT_DIR}/scripts/08-deploy-main-server.sh" ]; then
    bash "${PROJECT_DIR}/scripts/08-deploy-main-server.sh"
else
    warn "08-deploy-main-server.sh не найден, обновляем вручную..."
    cd "$PROJECT_DIR"
    git pull origin main || warn "Git pull не удался"
    docker compose down
    docker compose up -d --build
    docker compose exec ufo-app alembic upgrade head 2>/dev/null || true
    docker image prune -f
    ok "Основной сервер обновлён (ручной режим)"
fi

# Получаем токен из .env для проверки нод
XRAY_NODE_TOKEN=""
ENV_FILE="${PROJECT_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    XRAY_NODE_TOKEN=$(grep "^XRAY_NODE_TOKEN=" "$ENV_FILE" | cut -d= -f2- || echo "")
fi

# ── 2. RU сервер ──
echo ""
sep
log "Шаг 2/4: Обновление RU сервера (193.163.203.40)..."
sep
echo ""

ssh root@193.163.203.40 "cd /opt/ufobzk/xray-node && git pull origin main 2>/dev/null || true && pip3 install -q -r requirements.txt 2>/dev/null || true && systemctl restart xray-node && systemctl restart xray && echo 'RU updated'" && ok "RU сервер обновлён" || warn "RU сервер — проблема при обновлении"

# Проверим health
if [ -n "$XRAY_NODE_TOKEN" ]; then
    HEALTH=$(ssh root@193.163.203.40 "curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer ${XRAY_NODE_TOKEN}' http://127.0.0.1:9090/health 2>/dev/null || echo '000'" 2>/dev/null)
    if [ "$HEALTH" = "200" ]; then
        ok "RU API отвечает (200 OK)"
    else
        warn "RU API вернул ${HEALTH}"
    fi
fi

# ── 3. Доп. NL сервер ──
echo ""
sep
log "Шаг 3/4: Обновление доп. NL сервера (91.229.105.51)..."
sep
echo ""

ssh root@91.229.105.51 "cd /opt/ufobzk/xray-node && git pull origin main 2>/dev/null || true && pip3 install -q -r requirements.txt 2>/dev/null || true && systemctl restart xray-node && systemctl restart xray && echo 'NL-2 updated'" && ok "Доп. NL сервер обновлён" || warn "Доп. NL сервер — проблема при обновлении"

if [ -n "$XRAY_NODE_TOKEN" ]; then
    HEALTH=$(ssh root@91.229.105.51 "curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer ${XRAY_NODE_TOKEN}' http://127.0.0.1:9090/health 2>/dev/null || echo '000'" 2>/dev/null)
    if [ "$HEALTH" = "200" ]; then
        ok "Доп. NL API отвечает (200 OK)"
    else
        warn "Доп. NL API вернул ${HEALTH}"
    fi
fi

# ── 4. Finland сервер ──
echo ""
sep
log "Шаг 4/4: Обновление Finland сервера (217.60.60.51)..."
sep
echo ""

ssh root@217.60.60.51 "cd /opt/ufobzk/xray-node && git pull origin main 2>/dev/null || true && pip3 install -q -r requirements.txt 2>/dev/null || true && systemctl restart xray-node && systemctl restart xray && echo 'FI updated'" && ok "Finland сервер обновлён" || warn "Finland сервер — проблема при обновлении"

if [ -n "$XRAY_NODE_TOKEN" ]; then
    HEALTH=$(ssh root@217.60.60.51 "curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer ${XRAY_NODE_TOKEN}' http://127.0.0.1:8080/health 2>/dev/null || echo '000'" 2>/dev/null)
    if [ "$HEALTH" = "200" ]; then
        ok "Finland API отвечает (200 OK)"
    else
        warn "Finland API вернул ${HEALTH}"
    fi
fi

# ── Итог ──
echo ""
sep
echo ""
echo -e "  ${GREEN}${BOLD}Все серверы обновлены!${NC}"
echo ""
echo -e "  ${BOLD}Проверьте:${NC}"
echo -e "  1. https://ufolabs.online — сайт открывается"
echo -e "  2. /admin — админка работает"
echo -e "  3. Синхронизируйте Xray через админку → Система → Синх. Xray"
echo ""
