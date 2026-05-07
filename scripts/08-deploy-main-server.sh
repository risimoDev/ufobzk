#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  08-deploy-main-server.sh — Обновление основного NL-сервера
# ═══════════════════════════════════════════════════════════════════════════
#
#  КОГДА ЗАПУСКАТЬ:
#    - После git pull с новыми изменениями
#    - При миграции БД
#    - При добавлении новых xray-node серверов
#
#  Запуск: sudo bash scripts/08-deploy-main-server.sh
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

if [ "$(id -u)" -ne 0 ]; then
    err "Запустите от root: sudo bash $0"
    exit 1
fi

PROJECT_DIR="${PROJECT_DIR:-/opt/ufobzk}"
ENV_FILE="${PROJECT_DIR}/.env"
BACKUP_DIR="${PROJECT_DIR}/backups/deploy-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_DIR"

cd "$PROJECT_DIR"

# ── 1. Бэкап ──
echo ""
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Обновление основного сервера                 ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
echo ""

log "Бэкап .env и БД..."
if [ -f "$ENV_FILE" ]; then
    cp "$ENV_FILE" "${BACKUP_DIR}/.env.bak"
    ok ".env сохранён"
fi

# Бэкап БД
DB_PATH="${PROJECT_DIR}/data/vpnbzk.db"
if [ -f "$DB_PATH" ]; then
    cp "$DB_PATH" "${BACKUP_DIR}/vpnbzk.db.bak"
    ok "БД сохранена"
else
    # Попробуем из Docker volume
    docker compose exec ufo-app sh -c "cat /project/data/vpnbzk.db" > "${BACKUP_DIR}/vpnbzk.db.bak" 2>/dev/null && ok "БД из контейнера сохранена" || warn "БД не найдена"
fi

# ── 2. Git pull ──
log "Получение обновлений из git..."
if git pull origin main; then
    ok "Git pull выполнен"
else
    warn "Git pull не удался — продолжаем с текущей версией"
fi

# ── 3. Проверка .env ──
log "Проверка .env..."
if [ ! -f "$ENV_FILE" ]; then
    err ".env не найден! Создайте его перед деплоем."
    exit 1
fi

# Проверяем XRAY_NODE_TOKEN
if ! grep -q "^XRAY_NODE_TOKEN=" "$ENV_FILE" || [ -z "$(grep "^XRAY_NODE_TOKEN=" "$ENV_FILE" | cut -d= -f2-)" ]; then
    warn "XRAY_NODE_TOKEN не найден или пуст — сгенерируем"
    NEW_TOKEN=$(openssl rand -hex 32)
    if grep -q "^XRAY_NODE_TOKEN=" "$ENV_FILE"; then
        sed -i "s/^XRAY_NODE_TOKEN=.*/XRAY_NODE_TOKEN=${NEW_TOKEN}/" "$ENV_FILE"
    else
        echo "XRAY_NODE_TOKEN=${NEW_TOKEN}" >> "$ENV_FILE"
    fi
    ok "Сгенерирован XRAY_NODE_TOKEN (скопируйте на все ноды): ${NEW_TOKEN}"
else
    ok "XRAY_NODE_TOKEN найден"
fi

# ── 4. Docker Compose rebuild ──
log "Пересборка и запуск сервисов..."
docker compose down
docker compose up -d --build

sleep 5

# ── 5. Проверка здоровья ──
log "Проверка ufo-app..."
if docker compose exec ufo-app python -c "
import urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8000/', timeout=5)
    print('OK')
except Exception as e:
    print('FAIL:', e)
" 2>/dev/null | grep -q "OK"; then
    ok "ufo-app отвечает"
else
    warn "ufo-app не ответил — проверьте: docker compose logs ufo-app"
fi

# ── 6. Alembic миграции ──
log "Проверка миграций Alembic..."
docker compose exec ufo-app alembic upgrade head 2>/dev/null && ok "Миграции применены" || warn "Миграции не применились (возможно уже актуальны)"

# ── 7. Синхронизация Xray ──
log "Синхронизация Xray конфига..."
docker compose exec ufo-app python -c "
from app.database import SessionLocal
from app.xray import sync_and_reload
db = SessionLocal()
try:
    result = sync_and_reload(db)
    print('Xray sync:', 'OK' if result else 'FAIL')
finally:
    db.close()
" 2>/dev/null || warn "Автоматическая синхронизация Xray не удалась — сделайте через /admin → Система → Синх. Xray"

# ── 8. Очистка ──
log "Очистка старых Docker-образов..."
docker image prune -f

# ── 9. Итог ──
echo ""
sep
echo ""
echo -e "  ${GREEN}${BOLD}Основной сервер обновлён!${NC}"
echo ""
echo -e "  ${BOLD}Сервер:${NC} $(grep "^NL_SERVER_IP=" "$ENV_FILE" | cut -d= -f2-) (основной NL)"
echo -e "  ${BOLD}Домен:${NC} $(grep "^DOMAIN=" "$ENV_FILE" | cut -d= -f2-)"
echo ""
echo -e "  ${BOLD}Что проверить:${NC}"
echo -e "  1. https://$(grep "^DOMAIN=" "$ENV_FILE" | cut -d= -f2-) — сайт открывается"
echo -e "  2. /admin — админка работает"
echo -e "  3. /api/health — API отвечает"
echo -e "  4. Telegram бот отвечает"
echo ""
echo -e "  ${BOLD}Добавьте новые xray-node серверы в админке:${NC}"
echo -e "  → /admin → Servers → Добавить сервер"
echo -e "  → Token: $(grep "^XRAY_NODE_TOKEN=" "$ENV_FILE" | cut -d= -f2-)"
echo ""
echo -e "  ${BOLD}Для деплоя xray-node на новые серверы:${NC}"
echo -e "  bash scripts/09-deploy-xray-node.sh <SERVER_IP> <API_PORT>"
echo ""
echo -e "  ${BOLD}Бэкап:${NC} ${BACKUP_DIR}"
echo ""
