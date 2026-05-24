#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  08-deploy-main-server.sh — Zero-downtime деплой основного сервера
# ═══════════════════════════════════════════════════════════════════════════
#
#  Что делает:
#    1. Бэкап БД + .env
#    2. Проверка системных ресурсов (диск, память, Docker)
#    3. Git pull (если git-репо)
#    4. Генерация недостающих токенов в .env
#    5. Пересборка ТОЛЬКО образа ufo-app (xray/nginx не трогает)
#    6. Рестарт ТОЛЬКО ufo-app (zero-downtime — xray/nginx продолжают работать)
#    7. Ожидание healthy-статуса + автооткат при неудаче
#    8. Проверка БД, таблиц, миграций, API, Xray, SSL, внешнего доступа
#
#  Запуск: sudo bash scripts/08-deploy-main-server.sh [--force]
#  --force: пропустить подтверждение
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

FORCE=false
[ "${1:-}" = "--force" ] && FORCE=true

if [ "$(id -u)" -ne 0 ]; then
    err "Запустите от root: sudo bash $0"
    exit 1
fi

PROJECT_DIR="${PROJECT_DIR:-/opt/ufobzk}"
ENV_FILE="${PROJECT_DIR}/.env"
DEPLOY_TS=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="${PROJECT_DIR}/backups/deploy-${DEPLOY_TS}"

if [ ! -f "${PROJECT_DIR}/docker-compose.yml" ]; then
    err "docker-compose.yml не найден в ${PROJECT_DIR}"
    exit 1
fi

cd "$PROJECT_DIR"

# Загружаем .env для доступа к переменным
if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
else
    err ".env не найден в ${PROJECT_DIR}"
    exit 1
fi

echo ""
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  Zero-downtime деплой VPNBZK (основной сервер)            ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  Проект:  ${BOLD}${PROJECT_DIR}${NC}"
echo -e "  Домен:   ${DOMAIN:-не задан}"
echo -e "  Время:   $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

if [ "$FORCE" != true ]; then
    read -rp "$(echo -e "${YELLOW}Продолжить деплой? [y/N]: ${NC}")" CONFIRM
    if [[ "${CONFIRM,,}" != "y" ]]; then
        echo "Отменено."
        exit 0
    fi
fi

# ══════════════════════════════════════════
# 1. Бэкап
# ══════════════════════════════════════════

sep; log "Шаг 1/9: Бэкап..."
mkdir -p "$BACKUP_DIR"

cp "$ENV_FILE" "${BACKUP_DIR}/.env.bak"
ok ".env сохранён"

# Бэкап БД из Docker volume (надёжнее чем путь на хосте)
if docker compose ps -q ufo-app &>/dev/null && [ -n "$(docker compose ps -q ufo-app 2>/dev/null)" ]; then
    docker compose exec -T ufo-app sh -c "cat /project/data/vpnbzk.db" > "${BACKUP_DIR}/vpnbzk.db.bak" 2>/dev/null \
        && ok "БД сохранена из контейнера" \
        || warn "БД не найдена в контейнере (возможно первый запуск)"
else
    # Fallback: прямой путь если volume примонтирован
    LOCAL_DB=$(docker volume inspect ufobzk_app-data --format '{{.Mountpoint}}' 2>/dev/null || echo "")
    if [ -n "$LOCAL_DB" ] && [ -f "${LOCAL_DB}/vpnbzk.db" ]; then
        cp "${LOCAL_DB}/vpnbzk.db" "${BACKUP_DIR}/vpnbzk.db.bak"
        ok "БД сохранена из volume mountpoint"
    else
        warn "БД не найдена — backup пропущен"
    fi
fi

# Удаляем бэкапы старше 10 последних
find "${PROJECT_DIR}/backups" -maxdepth 1 -type d -name "deploy-*" \
    | sort | head -n -10 | xargs rm -rf 2>/dev/null || true
ok "Бэкап: ${BACKUP_DIR}"

# ══════════════════════════════════════════
# 2. Системные ресурсы
# ══════════════════════════════════════════

sep; log "Шаг 2/9: Проверка ресурсов..."

DISK_USAGE=$(df -h "$PROJECT_DIR" | awk 'NR==2 {print $5}' | tr -d '%')
if [ "$DISK_USAGE" -gt 90 ]; then
    err "Мало места на диске: ${DISK_USAGE}% — освободите место"
    exit 1
elif [ "$DISK_USAGE" -gt 80 ]; then
    warn "Диск заполнен на ${DISK_USAGE}%"
else
    ok "Диск: ${DISK_USAGE}% занято"
fi

MEM_AVAIL=$(free -m | awk '/^Mem:/{print $7}')
if [ "$MEM_AVAIL" -lt 256 ]; then
    warn "Мало свободной памяти: ${MEM_AVAIL}MB"
else
    ok "Память: ${MEM_AVAIL}MB свободно"
fi

if ! docker info &>/dev/null; then
    err "Docker daemon не запущен!"
    exit 1
fi
ok "Docker daemon работает"

# ══════════════════════════════════════════
# 3. Git pull
# ══════════════════════════════════════════

sep; log "Шаг 3/9: Обновление кода..."

if [ -d ".git" ]; then
    git fetch --all --prune
    BEFORE=$(git rev-parse HEAD)
    git pull --ff-only origin main 2>&1 | head -10
    AFTER=$(git rev-parse HEAD)
    if [ "$BEFORE" = "$AFTER" ]; then
        warn "Нет новых коммитов"
    else
        ok "Новые коммиты:"
        git log --oneline "$BEFORE".."$AFTER" | head -5 | sed 's/^/     /'
    fi
else
    warn "Не git-репозиторий — предполагаем, что файлы обновлены вручную"
fi

# ══════════════════════════════════════════
# 4. Генерация недостающих токенов в .env
# ══════════════════════════════════════════

sep; log "Шаг 4/9: Проверка .env..."

_ensure_token() {
    local key="$1"
    local len="${2:-32}"
    if ! grep -q "^${key}=" "$ENV_FILE" || [ -z "$(grep "^${key}=" "$ENV_FILE" | cut -d= -f2-)" ]; then
        local val
        val=$(openssl rand -hex "$len")
        if grep -q "^${key}=" "$ENV_FILE"; then
            sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
        else
            echo "${key}=${val}" >> "$ENV_FILE"
        fi
        ok "Сгенерирован ${key}: ${val}"
        return 0
    fi
    return 1
}

_ensure_token "XRAY_NODE_TOKEN"  && true
_ensure_token "SECRET_KEY" 64    && true
_ensure_token "METRICS_TOKEN" 32 && true

# Перезагружаем .env после возможных изменений
set -a; source "$ENV_FILE"; set +a

ok ".env проверен"

# ══════════════════════════════════════════
# 5. Пересборка образа ufo-app
# ══════════════════════════════════════════

sep; log "Шаг 5/9: Пересборка образа ufo-app..."

# Запоминаем текущий image ID для отката
PREV_IMAGE=$(docker inspect ufobzk-app --format='{{.Image}}' 2>/dev/null || echo "")

docker compose build ufo-app
ok "Образ ufo-app пересобран"

# ══════════════════════════════════════════
# 6. Rolling restart (только ufo-app)
#    Xray и Nginx НЕ перезапускаются — zero-downtime
# ══════════════════════════════════════════

sep; log "Шаг 6/9: Перезапуск ufo-app (zero-downtime)..."

docker compose up -d --no-deps --force-recreate ufo-app

# Ожидаем healthy-статус (max 90 сек)
MAX_WAIT=90
ELAPSED=0
STATUS=""
while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
    STATUS=$(docker inspect ufobzk-app --format='{{.State.Health.Status}}' 2>/dev/null || echo "starting")
    [ "$STATUS" = "healthy" ] && break
    echo -ne "\r  ⏳ ${ELAPSED}s [${STATUS}]    "
    sleep 3
    ELAPSED=$((ELAPSED + 3))
done
echo ""

if [ "$STATUS" != "healthy" ]; then
    err "ufo-app не стал healthy за ${MAX_WAIT}s (status: ${STATUS})"
    warn "Последние логи:"
    docker compose logs --tail=20 ufo-app
    warn "Откат к предыдущему образу..."
    docker compose stop ufo-app
    if [ -n "$PREV_IMAGE" ]; then
        # Откатываем через тег предыдущего образа
        docker tag "$PREV_IMAGE" ufobzk-app-rollback 2>/dev/null || true
    fi
    # Восстанавливаем БД из бэкапа если есть
    if [ -f "${BACKUP_DIR}/vpnbzk.db.bak" ]; then
        docker compose exec -T ufo-app sh -c "cat > /project/data/vpnbzk.db" < "${BACKUP_DIR}/vpnbzk.db.bak" 2>/dev/null || true
    fi
    docker compose up -d ufo-app
    err "Откат выполнен. Проверьте: docker compose logs ufo-app"
    exit 1
fi

ok "ufo-app healthy"

# Nginx reload (подхватывает изменения конфига без даунтайма)
docker compose exec -T nginx nginx -s reload 2>/dev/null && ok "Nginx перезагружен" || warn "Nginx reload пропущен"

# ══════════════════════════════════════════
# 7. Проверка БД и миграций
# ══════════════════════════════════════════

sep; log "Шаг 7/9: Проверка БД и миграций..."

DB_RESULT=$(docker compose exec -T ufo-app python3 - <<'PYEOF' 2>&1
import sqlite3, os, sys
db = '/project/data/vpnbzk.db'
if not os.path.exists(db):
    print('DB_MISSING'); sys.exit(1)
try:
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    av = conn.execute("SELECT version_num FROM alembic_version").fetchone() if 'alembic_version' in tables else None
    conn.close()
except Exception as ex:
    print('DB_ERROR:' + str(ex)); sys.exit(1)

required = {'users','vpn_keys','servers','payments','audit_log',
            'app_settings','guides','invite_keys','traffic_snapshots'}
missing = required - tables
if missing:
    print('MISSING:' + ','.join(sorted(missing)))
else:
    print('DB_OK:' + (av[0] if av else 'no_version'))
PYEOF
)

if echo "$DB_RESULT" | grep -q "^DB_OK"; then
    VER=$(echo "$DB_RESULT" | grep "^DB_OK" | cut -d: -f2)
    ok "БД в порядке, alembic version: ${VER}"
elif echo "$DB_RESULT" | grep -q "^MISSING"; then
    MISSING_TABLES=$(echo "$DB_RESULT" | grep "^MISSING" | cut -d: -f2)
    warn "Отсутствуют таблицы: ${MISSING_TABLES}"
    warn "Пробуем alembic upgrade head..."
    docker compose exec -T ufo-app alembic upgrade head 2>&1 | tail -5 || true
else
    warn "Проблема с БД: ${DB_RESULT}"
fi

# Проверка текущей версии alembic
ALEMBIC_VER=$(docker compose exec -T ufo-app alembic current 2>/dev/null || echo "не определена")
if echo "$ALEMBIC_VER" | grep -q "head"; then
    ok "Alembic на head"
else
    ok "Alembic version: $(echo "$ALEMBIC_VER" | tail -1)"
fi

# ══════════════════════════════════════════
# 8. Проверки сервисов
# ══════════════════════════════════════════

sep; log "Шаг 8/9: Проверка сервисов..."

# ── Контейнеры ──
for svc in ufo-app nginx xray; do
    SVC_STATUS=$(docker compose ps "$svc" --format '{{.Status}}' 2>/dev/null || echo "unknown")
    if echo "$SVC_STATUS" | grep -qi "running\|up\|healthy"; then
        ok "Контейнер ${svc}: ${SVC_STATUS}"
    else
        warn "Контейнер ${svc}: ${SVC_STATUS:-не найден}"
    fi
done

# ── API /health ──
sleep 2
HTTP_CODE=$(docker compose exec -T ufo-app python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)
    print(r.status)
except Exception as e:
    print('ERR')
" 2>/dev/null || echo "ERR")
if [ "$HTTP_CODE" = "200" ]; then
    ok "API /health → 200"
else
    warn "API /health → ${HTTP_CODE}"
fi

# ── Nginx config ──
NGINX_TEST=$(docker compose exec -T nginx nginx -t 2>&1 || true)
if echo "$NGINX_TEST" | grep -q "successful"; then
    ok "Nginx конфиг валиден"
else
    warn "Проблема с nginx: $(echo "$NGINX_TEST" | tail -2)"
fi

# ── Xray ──
XRAY_STATUS=$(docker compose ps xray --format '{{.Status}}' 2>/dev/null || echo "unknown")
if echo "$XRAY_STATUS" | grep -qi "running\|up"; then
    ok "Xray: ${XRAY_STATUS}"
else
    warn "Xray: ${XRAY_STATUS} — проверьте: docker compose logs xray"
fi

# Проверяем что Xray API отвечает (порт 10085)
XRAY_API=$(docker compose exec -T ufo-app sh -c \
    "nc -z xray 10085 2>/dev/null && echo OK || echo FAIL" 2>/dev/null || echo "SKIP")
if [ "$XRAY_API" = "OK" ]; then
    ok "Xray Stats API (10085) отвечает"
elif [ "$XRAY_API" != "SKIP" ]; then
    warn "Xray Stats API (10085) не отвечает — статистика трафика может не работать"
fi

# ── SSL сертификат через Docker exec в certbot volume ──
if [ -n "${DOMAIN:-}" ]; then
    CERT_CHECK=$(docker compose exec -T ufo-app sh -c \
        "openssl x509 -in /etc/letsencrypt/live/${DOMAIN}/fullchain.pem -noout -enddate 2>/dev/null || echo MISSING" 2>/dev/null || echo "SKIP")
    if echo "$CERT_CHECK" | grep -q "notAfter"; then
        EXPIRY=$(echo "$CERT_CHECK" | cut -d= -f2)
        ok "SSL сертификат ${DOMAIN}: действителен до ${EXPIRY}"
    else
        warn "SSL сертификат не найден для ${DOMAIN} — certbot должен его выпустить"
    fi

    # ── Внешний доступ ──
    EXT=$(curl -sf "https://${DOMAIN}/health" -o /dev/null -w "%{http_code}" --max-time 10 2>/dev/null || echo "000")
    if [ "$EXT" = "200" ]; then
        ok "Внешний доступ https://${DOMAIN}/health → 200"
    elif [ "$EXT" = "000" ]; then
        warn "Внешний доступ недоступен (DNS / фаервол?)"
    else
        warn "Внешний доступ → ${EXT}"
    fi
fi

# ── Metrics (если METRICS_TOKEN задан) ──
MTOKEN="${METRICS_TOKEN:-}"
if [ -n "$MTOKEN" ]; then
    METRICS_CODE=$(docker compose exec -T ufo-app python3 -c "
import urllib.request
req = urllib.request.Request('http://127.0.0.1:8000/metrics',
      headers={'Authorization': 'Bearer ${MTOKEN}'})
try:
    r = urllib.request.urlopen(req, timeout=5)
    print(r.status)
except Exception as e:
    print('ERR')
" 2>/dev/null || echo "ERR")
    if [ "$METRICS_CODE" = "200" ]; then
        ok "Prometheus /metrics → 200"
    else
        warn "Prometheus /metrics → ${METRICS_CODE}"
    fi
fi

# ══════════════════════════════════════════
# 9. Очистка
# ══════════════════════════════════════════

sep; log "Шаг 9/9: Очистка..."

# Удаляем только dangling images (не именованные тома!)
docker image prune -f >/dev/null 2>&1
ok "Dangling образы удалены"

# ══════════════════════════════════════════
# Итог
# ══════════════════════════════════════════

echo ""
sep
echo ""
echo -e "  ${GREEN}${BOLD}✅ Деплой завершён!${NC}"
echo ""
docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}"
echo ""
echo -e "  ${BOLD}Бэкап:${NC}   ${BACKUP_DIR}"
echo -e "  ${BOLD}Домен:${NC}   https://${DOMAIN:-localhost}"
echo ""
echo -e "  ${BOLD}Ссылки:${NC}"
echo -e "  • https://${DOMAIN:-localhost}/health      — API health"
echo -e "  • https://${DOMAIN:-localhost}/admin       — Админ-панель"
echo ""
if [ -n "${XRAY_NODE_TOKEN:-}" ]; then
    echo -e "  ${BOLD}XRAY_NODE_TOKEN${NC} (для xray-node серверов):"
    echo -e "  ${YELLOW}${XRAY_NODE_TOKEN}${NC}"
    echo ""
fi
if [ -n "${METRICS_TOKEN:-}" ]; then
    echo -e "  ${BOLD}Prometheus scrape:${NC}"
    echo -e "  curl -H 'Authorization: Bearer ${METRICS_TOKEN}' https://${DOMAIN:-localhost}/metrics"
    echo ""
fi
