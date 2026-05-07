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
elif docker compose ps -q ufo-app &>/dev/null && [ -n "$(docker compose ps -q ufo-app 2>/dev/null)" ]; then
    # Попробуем из Docker volume если контейнер запущен
    docker compose exec -T ufo-app sh -c "cat /project/data/vpnbzk.db" > "${BACKUP_DIR}/vpnbzk.db.bak" 2>/dev/null && ok "БД из контейнера сохранена" || warn "БД не найдена в контейнере"
else
    warn "БД не найдена локально и контейнер не запущен — backup пропущен"
fi

# ── 1.5. Проверка системных ресурсов ──
log "Проверка системных ресурсов..."

# Диск
DISK_USAGE=$(df -h "$PROJECT_DIR" | awk 'NR==2 {print $5}' | tr -d '%')
if [ "$DISK_USAGE" -gt 90 ]; then
    err "Мало места на диске: ${DISK_USAGE}% — освободите место перед деплоем"
    exit 1
elif [ "$DISK_USAGE" -gt 80 ]; then
    warn "Диск заполнен на ${DISK_USAGE}%"
else
    ok "Диск: ${DISK_USAGE}% занято"
fi

# Память
MEM_AVAIL=$(free -m | awk '/^Mem:/{print $7}')
if [ "$MEM_AVAIL" -lt 256 ]; then
    warn "Мало свободной памяти: ${MEM_AVAIL}MB"
else
    ok "Память: ${MEM_AVAIL}MB свободно"
fi

# Docker daemon
if ! docker info &>/dev/null; then
    err "Docker daemon не запущен!"
    exit 1
fi
ok "Docker daemon работает"

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

# Применяем миграции Alembic явно и перезапускаем ufo-app
# (чтобы lifespan подхватил актуальную схему при старте)
log "Применение миграций Alembic..."

# Если alembic_version отсутствует, но таблицы уже существуют (schema создана через create_all),
# проставляем stamp head — иначе alembic пытается пересоздать существующие таблицы.
# Используем sqlite3 из stdlib (не SQLAlchemy) для надёжности.
STAMP_RESULT=$(docker compose exec -T ufo-app python3 -c "
import sqlite3, os, sys
db = '/project/data/vpnbzk.db'
if not os.path.exists(db):
    print('FRESH'); sys.exit(0)
try:
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()}
    av_rows = 0
    if 'alembic_version' in tables:
        av_rows = conn.execute('SELECT COUNT(*) FROM alembic_version').fetchone()[0]
    conn.close()
except Exception as ex:
    print('ERR:' + str(ex)); sys.exit(1)
if 'users' not in tables:
    print('FRESH')
elif 'alembic_version' not in tables or av_rows == 0:
    print('STAMP_NEEDED')
else:
    print('HAS_TRACKING')
" 2>&1 || echo "EXEC_FAILED")

if [ "$STAMP_RESULT" = "STAMP_NEEDED" ]; then
    warn "БД без alembic tracking — проставляем stamp head..."
    STAMP_OUT=$(docker compose exec -T ufo-app alembic stamp head 2>&1 || true)
    if echo "$STAMP_OUT" | grep -qi "error\|fail\|traceback"; then
        warn "alembic stamp не удался: $STAMP_OUT"
    else
        ok "Alembic stamped to head"
    fi
elif echo "$STAMP_RESULT" | grep -q "ERR:\|EXEC_FAILED"; then
    warn "Проверка alembic tracking не удалась: $STAMP_RESULT"
fi

MIGRATE_UP=$(docker compose exec -T ufo-app alembic upgrade head 2>&1 || true)
if echo "$MIGRATE_UP" | grep -qi "error\|fail\|traceback"; then
    warn "Alembic upgrade не удался:"
    echo "$MIGRATE_UP" | tail -10
else
    ok "Alembic миграции применены"
fi

# ── Создание admin-пользователя если в БД нет ни одного ──
log "Проверка наличия admin-пользователя..."
ADMIN_USERNAME_ENV=$(grep -s "^ADMIN_USERNAME=" "$ENV_FILE" | cut -d= -f2- | tr -d '\r' || true)
ADMIN_PASSWORD_ENV=$(grep -s "^ADMIN_PASSWORD=" "$ENV_FILE" | cut -d= -f2- | tr -d '\r' || true)

if [ -n "$ADMIN_USERNAME_ENV" ] && [ -n "$ADMIN_PASSWORD_ENV" ]; then
    CREATE_RESULT=$(docker compose exec -T ufo-app python3 -c "
import sys
sys.path.insert(0, '/project')
from app.models import SessionLocal, User
from app.auth import hash_password
from app.models import _gen_uuid
db = SessionLocal()
try:
    admin_count = db.query(User).filter(User.is_admin == True).count()
    if admin_count > 0:
        print('ADMIN_EXISTS')
        sys.exit(0)
    u = User(
        username='${ADMIN_USERNAME_ENV}',
        password_hash=hash_password('${ADMIN_PASSWORD_ENV}'),
        display_name='Admin',
        is_admin=True,
        is_active=True,
        sub_token=_gen_uuid(),
    )
    db.add(u)
    db.commit()
    print('ADMIN_CREATED')
except Exception as e:
    print('ERR:' + str(e))
finally:
    db.close()
" 2>&1 || echo "CREATE_FAILED")
    if echo "$CREATE_RESULT" | grep -q "ADMIN_CREATED"; then
        ok "Admin-пользователь создан: $ADMIN_USERNAME_ENV"
    elif echo "$CREATE_RESULT" | grep -q "ADMIN_EXISTS"; then
        ok "Admin уже существует в БД"
    else
        warn "Создание admin не удалось: $CREATE_RESULT"
    fi
else
    warn "ADMIN_USERNAME или ADMIN_PASSWORD не задан в .env — admin-пользователь не создан автоматически"
fi

log "Перезапуск ufo-app для загрузки актуальной схемы..."
docker compose restart ufo-app
sleep 3

# ── 5. Проверка контейнеров ──
log "Проверка состояния контейнеров..."
FAILED_CONTAINERS=$(docker compose ps --format json 2>/dev/null | grep -v '"State":"running"' | grep -v 'running' || true)
if [ -n "$FAILED_CONTAINERS" ]; then
    warn "Некоторые контейнеры не запущены:"
    docker compose ps | grep -v "running" || true
else
    ok "Все контейнеры запущены"
fi

# Проверка каждого сервиса
for svc in ufo-app nginx xray certbot; do
    if docker compose ps -q "$svc" &>/dev/null && [ -n "$(docker compose ps -q "$svc" 2>/dev/null)" ]; then
        if docker compose ps "$svc" | grep -q "running"; then
            ok "Контейнер ${svc}: running"
        else
            warn "Контейнер ${svc}: не running — проверьте: docker compose logs ${svc}"
        fi
    else
        warn "Контейнер ${svc}: не найден"
    fi
done

# ── 6. Проверка nginx ──
log "Проверка nginx..."
NGINX_STATUS=$(docker compose exec nginx nginx -t 2>&1 || true)
if echo "$NGINX_STATUS" | grep -q "successful"; then
    ok "Nginx конфиг валиден"
else
    warn "Проблема с nginx конфигом:"
    echo "$NGINX_STATUS" | tail -5
fi

# Проверка nginx отвечает
if docker compose exec nginx curl -sf http://127.0.0.1/ &>/dev/null; then
    ok "Nginx отвечает (HTTP 200)"
else
    warn "Nginx не отвечает на localhost"
fi

# ── 7. Проверка БД и миграций ──
log "Проверка БД и миграций..."

# Проверим подключение к БД и наличие таблиц (через sqlite3, без app.database)
DB_CHECK=$(docker compose exec -T ufo-app python3 -c "
import sqlite3, os, sys
db = '/project/data/vpnbzk.db'
if not os.path.exists(db):
    print('DB_ERROR:file_not_found'); sys.exit(1)
try:
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()}
    conn.close()
except Exception as ex:
    print('DB_ERROR:' + str(ex)); sys.exit(1)
required = {'users', 'vpn_keys', 'servers', 'payments', 'audit_log', 'app_settings', 'guides', 'invite_keys'}
missing = required - tables
if missing:
    print('MISSING_TABLES:' + ' '.join(missing)); sys.exit(1)
print('DB_OK')
" 2>&1 || echo "DB_CHECK_FAILED")

if echo "$DB_CHECK" | grep -q "DB_OK"; then
    ok "БД подключена, все таблицы на месте"
else
    warn "Проблема с БД: $DB_CHECK"
fi

# Alembic миграции — проверим версию
log "Проверка миграций Alembic..."
MIGRATION_RESULT=$(docker compose exec -T ufo-app alembic current 2>/dev/null || echo "NONE")
if echo "$MIGRATION_RESULT" | grep -q "head"; then
    ok "Alembic на актуальной версии (head)"
else
    # Пробуем накатить миграции
    MIGRATE_OUTPUT=$(docker compose exec -T ufo-app alembic upgrade head 2>&1 || true)
    if echo "$MIGRATE_OUTPUT" | grep -qi "error\|fail\|traceback"; then
        warn "Ошибка при миграции:"
        echo "$MIGRATE_OUTPUT" | tail -10
    else
        ok "Миграции применены"
    fi
fi

# Повторная проверка таблиц после миграций
DB_CHECK2=$(docker compose exec -T ufo-app python3 -c "
import sqlite3, os
db = '/project/data/vpnbzk.db'
if not os.path.exists(db):
    print('MISSING:db_not_found')
else:
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()}
    conn.close()
    required = {'users', 'vpn_keys', 'servers', 'payments', 'audit_log', 'app_settings', 'guides', 'invite_keys'}
    missing = required - tables
    if missing:
        print('MISSING:' + ' '.join(missing))
    else:
        print('ALL_TABLES_OK')
" 2>&1 || echo "CHECK_FAILED")

if echo "$DB_CHECK2" | grep -q "ALL_TABLES_OK"; then
    ok "Все таблицы присутствуют после миграций"
else
    warn "Всё ещё отсутствуют таблицы: $DB_CHECK2"
fi

# ── 8. Проверка API /health ──
log "Проверка API (health endpoint)..."
sleep 2
HEALTH_STATUS=$(docker compose exec -T ufo-app curl -sf http://127.0.0.1:8000/api/health -o /dev/null -w "%{http_code}" 2>/dev/null || echo "000")
if [ "$HEALTH_STATUS" = "200" ]; then
    ok "API /api/health отвечает 200"
elif [ "$HEALTH_STATUS" = "000" ]; then
    # Пробуем через python
    HEALTH_PY=$(docker compose exec -T ufo-app python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)
    print(r.status)
except Exception as e:
    print(f'ERROR:{e}')
" 2>/dev/null || echo "PY_FAIL")
    if echo "$HEALTH_PY" | grep -q "200"; then
        ok "API /api/health отвечает 200 (Python check)"
    else
        warn "API /api/health не отвечает: $HEALTH_PY"
    fi
else
    warn "API /api/health вернул ${HEALTH_STATUS}"
fi

# ── 9. Проверка Xray контейнера ──
log "Проверка Xray контейнера..."
XRAY_PS=$(docker compose ps xray --format '{{.Status}}' 2>/dev/null || echo "unknown")
if echo "$XRAY_PS" | grep -qi "running\|up"; then
    ok "Xray контейнер запущен"
else
    warn "Xray контейнер не running: $XRAY_PS"
fi

# Проверим что Xray слушает порт 2053 (REALITY)
if docker compose exec -T xray sh -c "nc -z 127.0.0.1 2053" 2>/dev/null || \
   docker run --rm --network container:ufobzk-xray busybox nc -z 127.0.0.1 2053 2>/dev/null; then
    ok "Xray слушает порт 2053"
else
    warn "Xray не отвечает на порту 2053 — возможно конфиг ещё не загружен"
fi

# ── 10. Проверка SSL сертификатов ──
log "Проверка SSL сертификатов..."
DOMAIN=$(grep "^DOMAIN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")
if [ -n "$DOMAIN" ] && [ -f "nginx/ssl/live/${DOMAIN}/fullchain.pem" ]; then
    CERT_EXPIRY=$(openssl x509 -in "nginx/ssl/live/${DOMAIN}/fullchain.pem" -noout -dates 2>/dev/null | grep notAfter | cut -d= -f2)
    CERT_DAYS=$(openssl x509 -in "nginx/ssl/live/${DOMAIN}/fullchain.pem" -noout -checkend 864000 >/dev/null 2>&1 && echo "10+" || echo "<10")
    if [ "$CERT_DAYS" = "10+" ]; then
        ok "SSL сертификат ${DOMAIN} действителен (>10 дней)"
    else
        warn "SSL сертификат ${DOMAIN} истекает через <10 дней — проверьте certbot"
    fi
else
    warn "SSL сертификат не найден для ${DOMAIN} — certbot должен его создать"
fi

# ── 11. Проверка внешнего доступа (опционально) ──
log "Проверка внешнего доступа..."
DOMAIN=$(grep "^DOMAIN=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")
if [ -n "$DOMAIN" ]; then
    EXT_STATUS=$(curl -sfI "https://${DOMAIN}/api/health" -o /dev/null -w "%{http_code}" --max-time 10 2>/dev/null || echo "000")
    if [ "$EXT_STATUS" = "200" ]; then
        ok "Внешний доступ к https://${DOMAIN}/api/health работает (200)"
    elif [ "$EXT_STATUS" = "000" ]; then
        warn "Не удалось проверить внешний доступ (возможно DNS/фаервол)"
    else
        warn "Внешний доступ вернул ${EXT_STATUS}"
    fi
fi

# ── 12. Синхронизация Xray ──
log "Синхронизация Xray конфига..."
SYNC_RESULT=$(docker compose exec -T ufo-app python3 -c "
from app.database import SessionLocal
from app.xray import sync_and_reload
db = SessionLocal()
try:
    result = sync_and_reload(db)
    print('XRAY_SYNC_OK' if result else 'XRAY_SYNC_FAIL')
finally:
    db.close()
" 2>/dev/null || echo "SYNC_FAILED")

if echo "$SYNC_RESULT" | grep -q "XRAY_SYNC_OK"; then
    ok "Xray конфиг синхронизирован"
else
    warn "Синхронизация Xray не удалась: $SYNC_RESULT"
    warn "Сделайте вручную: /admin → Система → Синх. Xray"
fi

# ── 13. Очистка ──
log "Очистка старых Docker-образов и dangling volume..."
docker image prune -f
docker volume prune -f &>/dev/null || true

# ── 14. Итог ──
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
