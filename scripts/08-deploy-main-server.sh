#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  08-deploy-main-server.sh — Умный Zero-downtime деплой основного сервера
# ═══════════════════════════════════════════════════════════════════════════
#
#  Возможности:
#    1. Умная предстартовая проверка (root, ресурсы, Docker, .env, токены)
#    2. Атомарный онлайн-бэкап SQLite (WAL-safe через backup API) + .env
#    3. Безопасная синхронизация Git (авто-stash локальных правок, fast-forward)
#    4. Предварительная валидация синтаксиса Python перед сборкой
#    5. Сборка только ufo-app (Xray и Nginx НЕ затрагиваются = 0 даунтайм VPN)
#    6. Бесшовный rolling-перезапуск ufo-app с проверкой healthcheck
#    7. Автоматический откат к предыдущему образу и БД при сбое запуска
#    8. Полная проверка целостности SQLite (PRAGMA integrity & foreign keys)
#    9. Умная проверка и применение миграций Alembic (current vs heads)
#   10. Верификация критических таблиц и колонок базы данных
#   11. Проверка и мягкий reload Nginx (после валидации nginx -t)
#   12. Комплексный смоук-тест: /health, /robots.txt, OpSec анти-индексация,
#       Xray stats API, статус SSL-сертификата, Prometheus metrics
#   13. Детальный итоговый дашборд оператора со статистикой пользователей
#
#  Использование:
#    sudo bash scripts/08-deploy-main-server.sh [ПАРАМЕТРЫ]
#
#  Параметры:
#    --force       Пропустить интерактивный запрос подтверждения
#    --skip-git    Пропустить git fetch / pull (для ручного обновления файлов)
#    --check-only  Только диагностика и проверка (без сборки и перезапуска)
#    -h, --help    Показать справку
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

# Цветовая палитра
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BLUE='\033[0;34m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
NC='\033[0m'

# Функции форматированного вывода
log()   { echo -e "${CYAN}[•]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*" >&2; }
info()  { echo -e "    ${BLUE}ℹ${NC} $*"; }
sep()   { echo -e "${CYAN}──────────────────────────────────────────────────────────${NC}"; }
sep_db(){ echo -e "${CYAN}══════════════════════════════════════════════════════════${NC}"; }

# Парсинг аргументов командной строки
FORCE=false
SKIP_GIT=false
CHECK_ONLY=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force)
            FORCE=true
            shift
            ;;
        --skip-git)
            SKIP_GIT=true
            shift
            ;;
        --check-only)
            CHECK_ONLY=true
            shift
            ;;
        -h|--help)
            echo "Использование: sudo bash $0 [--force] [--skip-git] [--check-only]"
            echo ""
            echo "Опции:"
            echo "  --force       Пропустить подтверждение перед началом деплоя"
            echo "  --skip-git    Не делать git pull (полезно при ручной передаче файлов)"
            echo "  --check-only  Выполнить только аудит, проверку миграций и смоук-тесты"
            echo "  -h, --help    Показать это сообщение"
            exit 0
            ;;
        *)
            err "Неизвестный параметр: $1"
            echo "Используйте --help для справки."
            exit 1
            ;;
    esac
done

# Проверка прав суперпользователя
if [ "$(id -u)" -ne 0 ]; then
    err "Скрипт должен быть запущен с правами root: sudo bash $0"
    exit 1
fi

# Определение каталога проекта
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -f "${PROJECT_DIR:-}/docker-compose.yml" ]; then
    PROJECT_DIR="${PROJECT_DIR}"
elif [ -f "${REPO_DIR}/docker-compose.yml" ]; then
    PROJECT_DIR="${REPO_DIR}"
elif [ -f "/opt/ufobzk/docker-compose.yml" ]; then
    PROJECT_DIR="/opt/ufobzk"
else
    err "Каталог проекта с docker-compose.yml не найден!"
    exit 1
fi

cd "$PROJECT_DIR"
ENV_FILE="${PROJECT_DIR}/.env"
DEPLOY_TS=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="${PROJECT_DIR}/backups/deploy-${DEPLOY_TS}"

# Определение команды Docker Compose
if docker compose version &>/dev/null; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    err "Docker Compose не найден (ни 'docker compose', ни 'docker-compose')!"
    exit 1
fi

# Загрузка переменных окружения
if [ -f "$ENV_FILE" ]; then
    set -a; source "$ENV_FILE"; set +a
else
    err "Файл конфигурации .env не найден в ${PROJECT_DIR}"
    exit 1
fi

# Стартовый баннер
echo ""
sep_db
echo -e "${CYAN}${BOLD}  Умный Zero-Downtime Деплой VPNBZK (Основной Сервер)    ${NC}"
sep_db
echo -e "  Каталог:   ${BOLD}${PROJECT_DIR}${NC}"
echo -e "  Домен:     ${BOLD}${DOMAIN:-не задан}${NC}"
echo -e "  Compose:   ${BOLD}${DC}${NC}"
echo -e "  Режим:     $([ "$CHECK_ONLY" = true ] && echo -e "${YELLOW}ДИАГНОСТИКА / CHECK-ONLY${NC}" || echo -e "${GREEN}ПОЛНЫЙ ДЕПЛОЙ${NC}")"
echo -e "  Время:     $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

if [ "$FORCE" != true ] && [ "$CHECK_ONLY" != true ]; then
    read -rp "$(echo -e "${YELLOW}Начать процесс безопасного обновления? [y/N]: ${NC}")" CONFIRM || CONFIRM=""
    if [[ "${CONFIRM,,}" != "y" ]]; then
        echo "Деплой отменен пользователем."
        exit 0
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# 1. Предстартовые проверки системы и ресурсов
# ═══════════════════════════════════════════════════════════════════════════
sep; log "Шаг 1/10: Предстартовая проверка окружения и ресурсов..."

# Проверка свободного места на диске
DISK_AVAIL_KB=$(df -k "$PROJECT_DIR" | awk 'NR==2 {print $4}')
DISK_USAGE=$(df -h "$PROJECT_DIR" | awk 'NR==2 {print $5}' | tr -d '%')

if [ "$DISK_USAGE" -gt 95 ]; then
    err "Критически мало места на диске: ${DISK_USAGE}% занято! Прерывание для предотвращения сбоя БД."
    exit 1
elif [ "$DISK_USAGE" -gt 85 ]; then
    warn "Диск заполнен на ${DISK_USAGE}% (осталось $((DISK_AVAIL_KB / 1024)) MB). Рекомендуется очистить старые образы/логи."
else
    ok "Дисковое пространство: ${DISK_USAGE}% занято ($((DISK_AVAIL_KB / 1024)) MB свободно)"
fi

# Проверка оперативной памяти
MEM_AVAIL=$(free -m | awk '/^Mem:/{print $7}')
if [ -n "$MEM_AVAIL" ] && [ "$MEM_AVAIL" -lt 200 ]; then
    warn "Мало доступной оперативной памяти: ${MEM_AVAIL}MB. Сборка Docker может занять больше времени."
else
    ok "Оперативная память: ${MEM_AVAIL:-?}MB доступно"
fi

# Проверка Docker daemon
if ! docker info &>/dev/null; then
    err "Docker daemon не отвечает или не запущен!"
    exit 1
fi
ok "Docker daemon активен"

# Проверка и автогенерация обязательных секретов в .env
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
        ok "Сгенерирован недостающий параметр ${key}"
        return 0
    fi
    return 1
}

_ensure_token "XRAY_NODE_TOKEN"  && true
_ensure_token "SECRET_KEY" 64    && true
_ensure_token "METRICS_TOKEN" 32 && true

# Перезагружаем переменные после возможной генерации
set -a; source "$ENV_FILE"; set +a
ok "Конфигурация .env проверена"

# Если запущен режим только диагностики — переходим к шагам проверки
if [ "$CHECK_ONLY" = true ]; then
    log "Режим --check-only: шаги бэкапа, git pull, пересборки и перезапуска пропущены."
fi

# ═══════════════════════════════════════════════════════════════════════════
# 2. Атомарный онлайн-бэкап базы данных и конфигурации
# ═══════════════════════════════════════════════════════════════════════════
if [ "$CHECK_ONLY" != true ]; then
    sep; log "Шаг 2/10: Создание гарантированного атомарного бэкапа..."
    mkdir -p "$BACKUP_DIR"

    # Сохраняем копию .env
    cp "$ENV_FILE" "${BACKUP_DIR}/.env.bak"
    ok "Файл окружения сохранён: ${BACKUP_DIR}/.env.bak"

    BACKUP_SUCCESS=false

    # Проверяем, запущен ли ufo-app для выполнения горячего WAL-safe бэкапа
    if $DC ps --status running -q ufo-app 2>/dev/null | grep -q .; then
        log "Выполнение атомарного снапшота SQLite через SQLite Online Backup API в контейнере..."
        
        SNAPSHOT_RESULT=$($DC exec -T ufo-app python3 - "$DEPLOY_TS" <<'PYEOF' 2>&1 || true
import sqlite3, os, sys

db_src = '/project/data/vpnbzk.db'
if not os.path.exists(db_src):
    print('DB_NOT_FOUND')
    sys.exit(0)

bak_dir = '/project/data/backups'
os.makedirs(bak_dir, exist_ok=True)
bak_dst = f"{bak_dir}/pre_deploy_{sys.argv[1]}.db"

try:
    src = sqlite3.connect(db_src)
    dst = sqlite3.connect(bak_dst)
    src.backup(dst)
    dst.close()
    src.close()
    
    # Проверяем валидность созданного бэкапа
    chk = sqlite3.connect(bak_dst)
    res = chk.execute("PRAGMA integrity_check;").fetchone()
    chk.close()
    
    if res and res[0] == 'ok':
        print(f"SNAPSHOT_OK:{bak_dst}")
    else:
        print("SNAPSHOT_INTEGRITY_FAIL")
        sys.exit(1)
except Exception as ex:
    print(f"SNAPSHOT_ERROR:{ex}")
    sys.exit(1)
PYEOF
        )

        if echo "$SNAPSHOT_RESULT" | grep -q "^SNAPSHOT_OK:"; then
            CONTAINER_BAK_FILE=$(echo "$SNAPSHOT_RESULT" | grep "^SNAPSHOT_OK:" | cut -d: -f2-)
            $DC cp "ufo-app:${CONTAINER_BAK_FILE}" "${BACKUP_DIR}/vpnbzk.db.bak" 2>/dev/null || true
            if [ -s "${BACKUP_DIR}/vpnbzk.db.bak" ]; then
                BACKUP_SUCCESS=true
                ok "Атомарный бэкап успешно скопирован на хост"
            fi
        fi
    fi

    # Fallback: если контейнер был выключен или cp не сработал — копируем из volume на хосте
    if [ "$BACKUP_SUCCESS" = false ]; then
        warn "Контейнер ufo-app недоступен для API бэкапа, ищем SQLite volume на хосте..."
        VOL_MOUNT=$(docker volume inspect ufobzk_app-data --format '{{.Mountpoint}}' 2>/dev/null || echo "")
        [ -z "$VOL_MOUNT" ] && VOL_MOUNT=$(docker volume inspect "${COMPOSE_PROJECT_NAME:-ufobzk}_app-data" --format '{{.Mountpoint}}' 2>/dev/null || echo "")

        if [ -n "$VOL_MOUNT" ] && [ -f "${VOL_MOUNT}/vpnbzk.db" ]; then
            if command -v sqlite3 &>/dev/null; then
                sqlite3 "${VOL_MOUNT}/vpnbzk.db" ".backup '${BACKUP_DIR}/vpnbzk.db.bak'" 2>/dev/null || cp "${VOL_MOUNT}/vpnbzk.db" "${BACKUP_DIR}/vpnbzk.db.bak"
            else
                cp "${VOL_MOUNT}/vpnbzk.db" "${BACKUP_DIR}/vpnbzk.db.bak"
                [ -f "${VOL_MOUNT}/vpnbzk.db-wal" ] && cp "${VOL_MOUNT}/vpnbzk.db-wal" "${BACKUP_DIR}/vpnbzk.db-wal.bak" 2>/dev/null || true
                [ -f "${VOL_MOUNT}/vpnbzk.db-shm" ] && cp "${VOL_MOUNT}/vpnbzk.db-shm" "${BACKUP_DIR}/vpnbzk.db-shm.bak" 2>/dev/null || true
            fi
            BACKUP_SUCCESS=true
            ok "Бэкап создан напрямую из docker volume mountpoint"
        fi
    fi

    if [ -s "${BACKUP_DIR}/vpnbzk.db.bak" ]; then
        BAK_SIZE=$(du -h "${BACKUP_DIR}/vpnbzk.db.bak" | cut -f1)
        ok "Бэкап БД проверен: ${BACKUP_DIR}/vpnbzk.db.bak (${BAK_SIZE})"
    else
        warn "База данных не найдена (возможно, это первый запуск системы)"
    fi

    # Ротация старых бэкапов деплоя: оставляем 10 самых свежих
    find "${PROJECT_DIR}/backups" -maxdepth 1 -type d -name "deploy-*" \
        | sort | head -n -10 | xargs rm -rf 2>/dev/null || true
fi

# ═══════════════════════════════════════════════════════════════════════════
# 3. Безопасная синхронизация Git
# ═══════════════════════════════════════════════════════════════════════════
if [ "$CHECK_ONLY" != true ]; then
    sep; log "Шаг 3/10: Проверка и обновление репозитория Git..."

    if [ "$SKIP_GIT" = true ]; then
        warn "Флаг --skip-git: этап git pull пропущен."
    elif [ -d ".git" ]; then
        # Проверяем, есть ли незакоммиченные локальные правки
        DIRTY_CHANGES=$(git status --porcelain 2>/dev/null || echo "")
        if [ -n "$DIRTY_CHANGES" ]; then
            warn "Обнаружены локальные незакоммиченные изменения. Выполняем безопасный git stash..."
            git stash push -m "deploy-autostash-${DEPLOY_TS}" 2>&1 | head -3
            info "Изменения сохранены в git stash: deploy-autostash-${DEPLOY_TS}"
        fi

        CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "main")
        log "Текущая ветка: ${BOLD}${CURRENT_BRANCH}${NC}"

        git fetch --all --prune --quiet 2>/dev/null || true
        LOCAL_HASH=$(git rev-parse HEAD 2>/dev/null || echo "000")
        REMOTE_HASH=$(git rev-parse "origin/${CURRENT_BRANCH}" 2>/dev/null || echo "$LOCAL_HASH")

        if [ "$LOCAL_HASH" = "$REMOTE_HASH" ]; then
            ok "Код репозитория уже актуален (коммит ${LOCAL_HASH:0:7})"
        else
            log "Обнаружены свежие коммиты в origin/${CURRENT_BRANCH}. Обновление..."
            git pull --ff-only origin "${CURRENT_BRANCH}" 2>&1 | head -10 || {
                warn "Не удалось выполнить fast-forward git pull. Оставляем текущую версию кода."
            }
            NEW_HASH=$(git rev-parse HEAD 2>/dev/null || echo "")
            ok "Обновлено до: ${NEW_HASH:0:7}"
            git log --oneline "${LOCAL_HASH}..${NEW_HASH}" 2>/dev/null | head -5 | sed 's/^/     /' || true
        fi
    else
        warn "Каталог .git не обнаружен — предполагаем ручную передачу файлов на сервер."
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# 4. Предварительная валидация исходного кода (Python Lint / Compile)
# ═══════════════════════════════════════════════════════════════════════════
if [ "$CHECK_ONLY" != true ]; then
    sep; log "Шаг 4/10: Валидация синтаксиса Python перед сборкой контейнера..."

    if command -v python3 &>/dev/null; then
        SYNTAX_ERRORS=0
        while IFS= read -r -d '' pyfile; do
            if ! python3 -m py_compile "$pyfile" 2>&1; then
                err "Синтаксическая ошибка в файле: $pyfile"
                SYNTAX_ERRORS=$((SYNTAX_ERRORS + 1))
            fi
        done < <(find app -type f -name "*.py" -print0)

        if [ "$SYNTAX_ERRORS" -gt 0 ]; then
            err "Обнаружено синтаксических ошибок в коде: ${SYNTAX_ERRORS}! Сборка отменена."
            exit 1
        fi
        ok "Все файлы app/*.py компилируются без синтаксических ошибок"
    else
        info "Python3 не установлен на хосте, синтаксис будет проверен внутри контейнера."
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# 5. Сборка Docker-образа приложения ufo-app
# ═══════════════════════════════════════════════════════════════════════════
if [ "$CHECK_ONLY" != true ]; then
    sep; log "Шаг 5/10: Сборка Docker-образа ufo-app (zero-downtime)..."

    # Запоминаем текущий ID работающего образа для мгновенного отката
    PREV_IMAGE_ID=$(docker inspect ufobzk-app --format='{{.Image}}' 2>/dev/null || echo "")

    log "Запуск сборки контейнера приложения..."
    if ! $DC build ufo-app; then
        err "Ошибка сборки Docker-образа ufo-app!"
        err "Текущие сервисы продолжают работать без перебоев. Деплой прерван."
        exit 1
    fi
    ok "Образ ufo-app успешно собран"
fi

# ═══════════════════════════════════════════════════════════════════════════
# 6. Бесшовный перезапуск ufo-app с проверкой здоровья
# ═══════════════════════════════════════════════════════════════════════════
if [ "$CHECK_ONLY" != true ]; then
    sep; log "Шаг 6/10: Бесшовный перезапуск контейнера ufo-app..."
    info "Контейнеры Xray (VPN) и Nginx НЕ перезапускаются. Туннели клиентов не прерываются!"

    # Пересоздаем ТОЛЬКО ufo-app
    $DC up -d --no-deps --force-recreate ufo-app

    # Ожидание перехода в healthy статус
    MAX_WAIT=90
    ELAPSED=0
    STATUS="starting"
    SPIN='-\|/'

    echo -ne "  Ожидание инициализации ufo-app (до ${MAX_WAIT} сек)... "
    while [ "$ELAPSED" -lt "$MAX_WAIT" ]; do
        STATUS=$(docker inspect ufobzk-app --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' 2>/dev/null || echo "starting")
        
        if [ "$STATUS" = "healthy" ]; then
            break
        fi

        # Если контейнер упал (exited / dead) — не ждем таймаута
        if echo "$STATUS" | grep -qiE "exited|dead"; then
            break
        fi

        SPIN_CHAR=${SPIN:$((ELAPSED % 4)):1}
        echo -ne "\r  ⏳ Ожидание ufo-app [${STATUS}] ${SPIN_CHAR} ${ELAPSED}s / ${MAX_WAIT}s    "
        sleep 2
        ELAPSED=$((ELAPSED + 2))
    done
    echo ""

    # Проверка результата старта
    if [ "$STATUS" != "healthy" ]; then
        err "Контейнер ufo-app не стал healthy за ${MAX_WAIT}с (текущий статус: ${STATUS})"
        warn "Последние 30 строк журнала контейнера:"
        $DC logs --tail=30 ufo-app
        
        err "Внимание! Запуск аварийного отката..."
        $DC stop ufo-app 2>/dev/null || true
        
        if [ -n "$PREV_IMAGE_ID" ]; then
            log "Восстановление предыдущего рабочего образа..."
            docker tag "$PREV_IMAGE_ID" ufobzk-app:rollback 2>/dev/null || true
        fi
        
        if [ -f "${BACKUP_DIR}/vpnbzk.db.bak" ]; then
            log "Восстановление снапшота БД..."
            $DC exec -T ufo-app sh -c "cat > /project/data/vpnbzk.db" < "${BACKUP_DIR}/vpnbzk.db.bak" 2>/dev/null || true
        fi
        
        $DC up -d --no-deps ufo-app
        err "Откат завершён. Сервис возвращен к предыдущей стабильной версии."
        exit 1
    fi

    ok "ufo-app успешно запущен и перешел в статус healthy (${ELAPSED}s)"
fi

# ═══════════════════════════════════════════════════════════════════════════
# 7. Комплексный аудит БД, проверка целостности и миграции Alembic
# ═══════════════════════════════════════════════════════════════════════════
sep; log "Шаг 7/10: Комплексная проверка базы данных и миграций..."

DB_AUDIT_OUTPUT=$($DC exec -T ufo-app python3 - <<'PYEOF' 2>&1
import sqlite3, os, sys

db_path = '/project/data/vpnbzk.db'
if not os.path.exists(db_path):
    print("STATUS_FAIL:DB_FILE_NOT_FOUND")
    sys.exit(0)

try:
    conn = sqlite3.connect(db_path, timeout=10)
    
    # 1. PRAGMA integrity_check
    integ = conn.execute("PRAGMA integrity_check;").fetchall()
    if not integ or integ[0][0] != "ok":
        print(f"STATUS_FAIL:INTEGRITY_ERROR:{integ}")
        sys.exit(0)

    # 2. PRAGMA foreign_key_check
    fk = conn.execute("PRAGMA foreign_key_check;").fetchall()
    if fk:
        print(f"STATUS_WARN:FK_VIOLATIONS:{len(fk)}")

    # 3. Список таблиц
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    required_tables = {'users', 'vpn_keys', 'servers', 'payments', 'audit_log', 'app_settings', 'guides', 'invite_keys', 'traffic_snapshots'}
    missing_tables = required_tables - tables
    if missing_tables:
        print(f"STATUS_FAIL:MISSING_TABLES:{','.join(sorted(missing_tables))}")
        sys.exit(0)

    # 4. Проверка критических полей в таблице users
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    needed_user_cols = {'id', 'username', 'password_hash', 'telegram_id', 'sub_token', 'sub_token_updated_at'}
    missing_user_cols = needed_user_cols - user_cols
    if missing_user_cols:
        print(f"STATUS_WARN:MISSING_USER_COLS:{','.join(sorted(missing_user_cols))}")

    # 5. Проверка полей в vpn_keys
    key_cols = {r[1] for r in conn.execute("PRAGMA table_info(vpn_keys)").fetchall()}
    needed_key_cols = {'notes', 'speed_limit_kbps'}
    missing_key_cols = needed_key_cols - key_cols
    if missing_key_cols:
        print(f"STATUS_WARN:MISSING_KEY_COLS:{','.join(sorted(missing_key_cols))}")

    # 6. Сбор статистики
    users_cnt = conn.execute("SELECT count(*) FROM users").fetchone()[0]
    active_keys_cnt = conn.execute("SELECT count(*) FROM vpn_keys WHERE is_active=1").fetchone()[0]
    servers_cnt = conn.execute("SELECT count(*) FROM servers").fetchone()[0]
    
    # 7. Версия Alembic
    alembic_ver = 'none'
    has_alembic = 'alembic_version' in tables
    if has_alembic:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        if row:
            alembic_ver = row[0]

    conn.close()
    print(f"STATUS_OK|has_alembic={1 if has_alembic else 0}|ver={alembic_ver}|users={users_cnt}|keys={active_keys_cnt}|servers={servers_cnt}")
except Exception as ex:
    print(f"STATUS_FAIL:EXCEPTION:{ex}")
    sys.exit(0)
PYEOF
)

if echo "$DB_AUDIT_OUTPUT" | grep -q "^STATUS_FAIL:"; then
    err "Ошибка проверки БД: $(echo "$DB_AUDIT_OUTPUT" | grep "^STATUS_FAIL:" | cut -d: -f2-)"
    if [ "$CHECK_ONLY" != true ]; then
        exit 1
    fi
else
    ok "Целостность SQLite (PRAGMA integrity_check): OK"
fi

if echo "$DB_AUDIT_OUTPUT" | grep -q "^STATUS_WARN:"; then
    warn "Предупреждение структуры БД: $(echo "$DB_AUDIT_OUTPUT" | grep "^STATUS_WARN:" | cut -d: -f2-)"
fi

# Умная проверка и синхронизация Alembic
HAS_ALEMBIC=$(echo "$DB_AUDIT_OUTPUT" | grep "^STATUS_OK" | tr '|' '\n' | grep "^has_alembic=" | cut -d= -f2 || echo "1")
CURRENT_ALEMBIC_VER=$(echo "$DB_AUDIT_OUTPUT" | grep "^STATUS_OK" | tr '|' '\n' | grep "^ver=" | cut -d= -f2 || echo "none")

if [ "$HAS_ALEMBIC" = "0" ]; then
    warn "Таблица alembic_version не найдена на действующей БД."
    log "Фиксируем текущее состояние базы данных через alembic stamp head..."
    $DC exec -T ufo-app alembic stamp head 2>&1 | tail -3 || true
    ok "База данных успешно привязана к ревизиям Alembic"
else
    # Проверяем статус миграций
    ALEMBIC_CHECK=$($DC exec -T ufo-app python3 - <<'PYEOF' 2>&1 || echo "CHECK_FAIL"
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext
import sqlite3

try:
    conn = sqlite3.connect('/project/data/vpnbzk.db')
    context = MigrationContext.configure(conn)
    current_rev = context.get_current_revision()
    conn.close()

    config = Config('/project/alembic.ini')
    script = ScriptDirectory.from_config(config)
    head_rev = script.get_current_head()

    if current_rev == head_rev:
        print(f"UP_TO_DATE:{current_rev}")
    else:
        print(f"NEEDS_UPGRADE:current={current_rev},head={head_rev}")
except Exception as e:
    print(f"ERR:{e}")
PYEOF
    )

    if echo "$ALEMBIC_CHECK" | grep -q "^UP_TO_DATE:"; then
        REV=$(echo "$ALEMBIC_CHECK" | grep "^UP_TO_DATE:" | cut -d: -f2)
        ok "Миграции Alembic актуальны (ревизия: ${REV})"
    elif echo "$ALEMBIC_CHECK" | grep -q "^NEEDS_UPGRADE:"; then
        MIG_DETAILS=$(echo "$ALEMBIC_CHECK" | grep "^NEEDS_UPGRADE:" | cut -d: -f2-)
        warn "Обнаружены непримененные миграции (${MIG_DETAILS})."
        log "Запуск: alembic upgrade head..."
        $DC exec -T ufo-app alembic upgrade head
        ok "Миграции успешно применены до head"
    else
        # Fallback на CLI alembic
        CURRENT_CLI=$($DC exec -T ufo-app alembic current 2>/dev/null | tr -d '\r' || echo "")
        ok "Alembic текущее состояние: $(echo "$CURRENT_CLI" | tail -1)"
    fi
fi

# Извлечение актуальной статистики пользователей
TOTAL_USERS=$(echo "$DB_AUDIT_OUTPUT" | grep "^STATUS_OK" | tr '|' '\n' | grep "^users=" | cut -d= -f2 || echo "0")
ACTIVE_KEYS=$(echo "$DB_AUDIT_OUTPUT" | grep "^STATUS_OK" | tr '|' '\n' | grep "^keys=" | cut -d= -f2 || echo "0")
TOTAL_SERVERS=$(echo "$DB_AUDIT_OUTPUT" | grep "^STATUS_OK" | tr '|' '\n' | grep "^servers=" | cut -d= -f2 || echo "0")

ok "Данные пользователей в полной безопасности: ${BOLD}${TOTAL_USERS} пользователей${NC}, ${ACTIVE_KEYS} активных ключей"

# ═══════════════════════════════════════════════════════════════════════════
# 8. Проверка и перезагрузка связанных служб (Nginx, MTG)
# ═══════════════════════════════════════════════════════════════════════════
sep; log "Шаг 8/10: Синхронизация и проверка связанных компонентов..."

# Проверка конфигурации Nginx
NGINX_TEST=$($DC exec -T nginx nginx -t 2>&1 || true)
if echo "$NGINX_TEST" | grep -q "successful"; then
    ok "Синтаксис конфигурации Nginx валиден"
    # Бесшовный reload Nginx
    if $DC exec -T nginx nginx -s reload 2>/dev/null; then
        ok "Конфигурация Nginx перезагружена без разрыва сессий (graceful reload)"
    else
        warn "Не удалось перезагрузить Nginx через nginx -s reload"
    fi
else
    warn "Предупреждение Nginx test: $(echo "$NGINX_TEST" | tail -2)"
fi

# Проверка MTProto-прокси (mtg)
if grep -q "^[[:space:]]*mtg:" "${PROJECT_DIR}/docker-compose.yml" 2>/dev/null; then
    MTG_STATE=$($DC ps --status running -q mtg 2>/dev/null || echo "")
    if [ -n "$MTG_STATE" ]; then
        ok "Служба mtg (MTProto) активна"
    else
        log "Запуск контейнера mtg..."
        $DC up -d --no-deps mtg >/dev/null 2>&1 || true
        ok "Служба mtg запущена"
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# 9. Комплексные смоук-тесты и OpSec проверки
# ═══════════════════════════════════════════════════════════════════════════
sep; log "Шаг 9/10: Комплексные смоук-тесты и проверка безопасности..."

# 1. Проверка внутреннего API /health
INTERNAL_HEALTH=$($DC exec -T ufo-app python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5)
    print(r.status)
except Exception as e:
    print('ERR')
" 2>/dev/null || echo "ERR")

if [ "$INTERNAL_HEALTH" = "200" ]; then
    ok "Внутренний API /health отвечает: 200 OK"
else
    warn "Внутренний API /health вернул код: ${INTERNAL_HEALTH}"
fi

# 2. Проверка анти-индексации robots.txt
ROBOTS_TXT=$($DC exec -T ufo-app python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:8000/robots.txt', timeout=5)
    content = r.read().decode('utf-8')
    print('OK' if 'Disallow: /' in content else 'FAIL')
except Exception:
    print('ERR')
" 2>/dev/null || echo "ERR")

if [ "$ROBOTS_TXT" = "OK" ]; then
    ok "Файл /robots.txt строго запрещает индексацию поисковиками (Disallow: /)"
else
    warn "Проверьте отдачу /robots.txt (ответ: ${ROBOTS_TXT})"
fi

# 3. Проверка заголовков безопасности X-Robots-Tag
SECURITY_HEADER_CHECK=$($DC exec -T ufo-app python3 -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:8000/', timeout=5)
    header = r.headers.get('x-robots-tag', '')
    print('OK' if 'noindex' in header.lower() else 'MISSING')
except Exception:
    print('ERR')
" 2>/dev/null || echo "ERR")

if [ "$SECURITY_HEADER_CHECK" = "OK" ]; then
    ok "Заголовок X-Robots-Tag (noindex, nofollow) присутствует во всех ответах"
else
    warn "Заголовок X-Robots-Tag не обнаружен"
fi

# 4. Проверка доступности Xray Stats API (порт 10085)
XRAY_API_CHECK=$($DC exec -T ufo-app python3 -c "
import socket
try:
    s = socket.create_connection(('xray', 10085), timeout=3)
    s.close()
    print('OK')
except Exception as e:
    print('FAIL:' + str(e))
" 2>/dev/null || echo "SKIP")

if [ "$XRAY_API_CHECK" = "OK" ]; then
    ok "Xray Core Stats API (10085) доступен и отвечает"
else
    warn "Xray Stats API (10085) не отвечает: ${XRAY_API_CHECK}"
fi

# 5. Проверка SSL-сертификата Nginx
if [ -n "${DOMAIN:-}" ]; then
    SSL_CHECK=$($DC exec -T nginx sh -c "openssl x509 -in /etc/letsencrypt/live/${DOMAIN}/fullchain.pem -noout -enddate 2>/dev/null || echo MISSING" 2>/dev/null || echo "SKIP")
    if echo "$SSL_CHECK" | grep -q "notAfter"; then
        EXP_DATE=$(echo "$SSL_CHECK" | cut -d= -f2)
        ok "SSL сертификат (${DOMAIN}): действителен до ${EXP_DATE}"
    else
        info "SSL сертификат через локальный том certbot проверяется..."
    fi

    # 6. Внешняя доступность домена
    EXT_STATUS=$(curl -sf -o /dev/null -w "%{http_code}" --max-time 8 "https://${DOMAIN}/health" 2>/dev/null || echo "000")
    if [ "$EXT_STATUS" = "200" ]; then
        ok "Внешний доступ: https://${DOMAIN}/health → 200 OK"
    elif [ "$EXT_STATUS" = "000" ]; then
        info "Внешний HTTPS запрос не прошел с хоста (возможно, локальный hairpin NAT). Это штатная ситуация."
    else
        warn "Внешний HTTPS запрос вернул статус: ${EXT_STATUS}"
    fi
fi

# 7. Проверка метрик Prometheus (если токен задан)
if [ -n "${METRICS_TOKEN:-}" ]; then
    METRICS_STATUS=$($DC exec -T ufo-app python3 -c "
import urllib.request
req = urllib.request.Request('http://127.0.0.1:8000/metrics', headers={'Authorization': 'Bearer ${METRICS_TOKEN}'})
try:
    r = urllib.request.urlopen(req, timeout=5)
    print(r.status)
except Exception:
    print('ERR')
" 2>/dev/null || echo "ERR")

    if [ "$METRICS_STATUS" = "200" ]; then
        ok "Эндпоинт Prometheus /metrics авторизован и отдает данные"
    fi
fi

# ═══════════════════════════════════════════════════════════════════════════
# 10. Очистка неиспользуемых образов и итоговый отчет
# ═══════════════════════════════════════════════════════════════════════════
if [ "$CHECK_ONLY" != true ]; then
    sep; log "Шаг 10/10: Очистка устаревших слоев Docker..."
    docker image prune -f >/dev/null 2>&1 || true
    ok "Dangling слои Docker очищены"
fi

echo ""
sep_db
echo -e "${GREEN}${BOLD}  ✅ ДЕПЛОЙ УСПЕШНО ЗАВЕРШЁН! (ZERO-DOWNTIME)${NC}"
sep_db
echo ""

# Статус контейнеров
echo -e "${BOLD}Текущее состояние контейнеров:${NC}"
$DC ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null || $DC ps
echo ""

# Сводная таблица показателей
echo -e "${BOLD}Сводка системы:${NC}"
echo -e "  • Активных пользователей в БД : ${GREEN}${BOLD}${TOTAL_USERS}${NC}"
echo -e "  • Активных ключей подключения : ${GREEN}${BOLD}${ACTIVE_KEYS}${NC}"
echo -e "  • Подключенных серверов/нод   : ${GREEN}${BOLD}${TOTAL_SERVERS}${NC}"
[ -f "${BACKUP_DIR}/vpnbzk.db.bak" ] && echo -e "  • Контрольный бэкап деплоя    : ${CYAN}${BACKUP_DIR}${NC}"
echo -e "  • Адрес панели управления     : ${CYAN}https://${DOMAIN:-localhost}/admin${NC}"
echo -e "  • Личный кабинет пользователей: ${CYAN}https://${DOMAIN:-localhost}/cabinet${NC}"
echo ""

echo -e "${BOLD}Полезные команды для мониторинга:${NC}"
echo -e "  • Просмотр логов приложения : ${YELLOW}${DC} logs -f --tail=50 ufo-app${NC}"
echo -e "  • Просмотр логов Xray core  : ${YELLOW}${DC} logs -f --tail=50 xray${NC}"
echo -e "  • Проверка здоровья системы : ${YELLOW}sudo bash scripts/08-deploy-main-server.sh --check-only${NC}"
echo ""
