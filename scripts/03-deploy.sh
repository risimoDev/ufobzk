#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  03-deploy.sh — Деплой обновлений (zero-downtime)
# ═══════════════════════════════════════════════════════════
#  Что делает:
#   • Бэкапит БД и .env перед обновлением
#   • Обновляет файлы проекта (git pull или rsync)
#   • Пересобирает только изменённые образы
#   • Rolling restart (приложение, затем nginx reload)
#   • Проверяет здоровье после деплоя
#   • Автоматический откат при ошибке
#
#  Запуск: sudo bash scripts/03-deploy.sh [--force]
#  Флаг --force: пропустить подтверждение
# ═══════════════════════════════════════════════════════════

set -euo pipefail

# ── Цвета ──
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

FORCE=false
[ "${1:-}" = "--force" ] && FORCE=true

# ── Определяем директорию проекта ──

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Если запущен из /opt/ufobzk/scripts — значит PROJECT_DIR = /opt/ufobzk
# Если запущен из клонированного репо — тоже ОК
if [ ! -f "docker-compose.yml" ]; then
    # Попробуем /opt/ufobzk
    if [ -f "/opt/ufobzk/docker-compose.yml" ]; then
        PROJECT_DIR="/opt/ufobzk"
        cd "$PROJECT_DIR"
    else
        err "docker-compose.yml не найден"
        exit 1
    fi
fi

if [ ! -f ".env" ]; then
    err ".env не найден. Сначала: bash scripts/02-install.sh"
    exit 1
fi

source .env

# Определяем compose file
if [ -f "/etc/letsencrypt/live/${DOMAIN:-}/fullchain.pem" ] 2>/dev/null || \
   docker volume ls 2>/dev/null | grep -q 'certbot-certs'; then
    COMPOSE_FILE="docker-compose.yml"
else
    COMPOSE_FILE="docker-compose.dev.yml"
fi

echo ""
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo -e "${CYAN}  Деплой обновлений — НИИ АЯ              ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo ""
echo -e "  Проект:   ${BOLD}${PROJECT_DIR}${NC}"
echo -e "  Compose:  ${COMPOSE_FILE}"
echo -e "  Домен:    ${DOMAIN:-localhost}"
echo ""

if [ "$FORCE" != true ]; then
    read -rp "$(echo -e "${YELLOW}Продолжить деплой? [y/N]: ${NC}")" CONFIRM
    if [[ "${CONFIRM,,}" != "y" ]]; then
        echo "Отменено."
        exit 0
    fi
fi

DEPLOY_TS=$(date '+%Y%m%d_%H%M%S')

# ══════════════════════════════════════════
# 1. Бэкап
# ══════════════════════════════════════════

BACKUP_DIR="$PROJECT_DIR/backups/$DEPLOY_TS"
mkdir -p "$BACKUP_DIR"

log "Бэкап перед деплоем..."

# Бэкап БД
if [ -f "data/vpnbzk.db" ]; then
    cp data/vpnbzk.db "$BACKUP_DIR/vpnbzk.db"
    ok "БД сохранена"
elif docker volume ls | grep -q 'app-data' 2>/dev/null; then
    docker run --rm -v vpnbzk_app-data:/data:ro -v "$BACKUP_DIR":/backup alpine \
        cp /data/vpnbzk.db /backup/vpnbzk.db 2>/dev/null && ok "БД сохранена (из volume)" || warn "БД не найдена в volume"
fi

# Бэкап .env
cp .env "$BACKUP_DIR/.env"
ok "Конфигурация сохранена"

# Запоминаем текущий образ (для отката)
PREV_IMAGE=$(docker inspect vpnbzk-app --format='{{.Image}}' 2>/dev/null || echo "")

# Удалить старые бэкапы (хранить 10)
if [ -d "$PROJECT_DIR/backups" ]; then
    ls -1dt "$PROJECT_DIR/backups"/*/ 2>/dev/null | tail -n +11 | xargs rm -rf 2>/dev/null || true
    ok "Бэкап: $BACKUP_DIR"
fi

# ══════════════════════════════════════════
# 2. Обновление кода
# ══════════════════════════════════════════

if [ -d ".git" ]; then
    log "Git pull..."
    git fetch --all --prune
    BEFORE=$(git rev-parse HEAD)
    git pull --ff-only
    AFTER=$(git rev-parse HEAD)
    if [ "$BEFORE" = "$AFTER" ]; then
        warn "Нет новых коммитов"
    else
        CHANGES=$(git log --oneline "$BEFORE".."$AFTER" | head -5)
        ok "Обновлено:"
        echo "$CHANGES" | sed 's/^/     /'
    fi
else
    warn "Не git-репозиторий — предполагаем, что файлы обновлены вручную"
fi

# ══════════════════════════════════════════
# 3. Пересборка
# ══════════════════════════════════════════

log "Сборка образов..."
docker compose -f "$COMPOSE_FILE" build --quiet
ok "Образы собраны"

# ══════════════════════════════════════════
# 4. Rolling restart
# ══════════════════════════════════════════

log "Перезапуск приложения..."

# Сначала пересоздаём app (быстрый контейнер)
docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate ufo-app

# Ждём healthy
log "Ожидание готовности приложения..."
MAX_WAIT=60
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT ]; do
    STATUS=$(docker inspect vpnbzk-app --format='{{.State.Health.Status}}' 2>/dev/null || echo "starting")
    if [ "$STATUS" = "healthy" ]; then
        break
    fi
    sleep 3
    ELAPSED=$((ELAPSED + 3))
    echo -ne "\r  ⏳ ${ELAPSED}s [${STATUS}]"
done
echo ""

# Проверка
STATUS=$(docker inspect vpnbzk-app --format='{{.State.Health.Status}}' 2>/dev/null || echo "unknown")
if [ "$STATUS" != "healthy" ]; then
    err "Приложение не стало healthy за ${MAX_WAIT}s (status: ${STATUS})"
    warn "Попытка отката..."

    if [ -n "$PREV_IMAGE" ]; then
        docker stop vpnbzk-app 2>/dev/null || true
        docker rm vpnbzk-app 2>/dev/null || true
        # Восстанавливаем БД
        if [ -f "$BACKUP_DIR/vpnbzk.db" ]; then
            cp "$BACKUP_DIR/vpnbzk.db" data/vpnbzk.db 2>/dev/null || true
        fi
        docker compose -f "$COMPOSE_FILE" up -d
        err "Откат выполнен. Проверьте логи: docker logs vpnbzk-app"
    fi
    exit 1
fi

ok "Приложение healthy"

# Если production — reload nginx
if [ "$COMPOSE_FILE" = "docker-compose.yml" ]; then
    docker compose exec nginx nginx -s reload 2>/dev/null && ok "Nginx перезагружен" || true
fi

# ══════════════════════════════════════════
# 5. Очистка
# ══════════════════════════════════════════

log "Очистка неиспользуемых образов..."
docker image prune -f --filter "until=48h" >/dev/null 2>&1
ok "Очистка завершена"

# ══════════════════════════════════════════
# 6. Итог
# ══════════════════════════════════════════

echo ""
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ Деплой завершён!                      ${NC}"
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo ""
docker compose -f "$COMPOSE_FILE" ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo -e "  Бэкап: ${BACKUP_DIR}"
echo -e "  Время: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""
