#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  07-migrate-nl-server.sh — Замена NL-сервера на новый
# ═══════════════════════════════════════════════════════════════════════════
#
#  КОГДА ЗАПУСКАТЬ:
#    - Провайдер заблокировал старый IP
#    - Нужен другой дата-центр / страна
#    - Перезамена по причине компрометации
#
#  ПОРЯДОК ЗАПУСКА:
#    1. Запустите ЭТОТ скрипт на СТАРОМ (основном) сервере — он создаёт бэкап
#    2. Запустите setup-nl-server.sh на НОВОМ сервере
#    3. Запустите ЭТОТ скрипт с флагом --apply на СТАРОМ сервере, введя новые данные
#
#  ИЛИ полностью автоматически:
#    bash 07-migrate-nl-server.sh --backup           # шаг 1: бэкап
#    bash 07-migrate-nl-server.sh --apply            # шаг 3: применить новые данные
#
#  Запуск: sudo bash scripts/07-migrate-nl-server.sh [--backup|--apply|--rollback]
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

# ── Конфиг пути ──
PROJECT_DIR="${PROJECT_DIR:-/opt/ufobzk}"
ENV_FILE="${PROJECT_DIR}/.env"
BACKUP_DIR="${PROJECT_DIR}/backups/nl-migration-$(date +%Y%m%d-%H%M%S)"
BACKUP_LATEST_LINK="${PROJECT_DIR}/backups/nl-migration-latest"

MODE="${1:-}"

# ════════════════════════════════════════════════════════════
#  Функция: создать бэкап
# ════════════════════════════════════════════════════════════
do_backup() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  Шаг 1/3: Создание резервной копии           ${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
    echo ""

    mkdir -p "$BACKUP_DIR"

    # Бэкап .env
    if [ -f "$ENV_FILE" ]; then
        cp "$ENV_FILE" "$BACKUP_DIR/.env.bak"
        ok "Сохранён .env → ${BACKUP_DIR}/.env.bak"
    else
        err ".env файл не найден: $ENV_FILE"
        err "Убедитесь что PROJECT_DIR правильный (текущий: $PROJECT_DIR)"
        exit 1
    fi

    # Бэкап xray конфига из Docker volume
    if docker compose -f "${PROJECT_DIR}/docker-compose.yml" exec ufo-app \
        cat /etc/xray/config.json > "${BACKUP_DIR}/xray-config.json.bak" 2>/dev/null; then
        ok "Сохранён xray config → ${BACKUP_DIR}/xray-config.json.bak"
    else
        warn "Не удалось скопировать xray конфиг (приложение не запущено?). Продолжаем..."
    fi

    # Бэкап БД
    DB_PATH="${PROJECT_DIR}/data/vpnbzk.db"
    if [ -f "$DB_PATH" ]; then
        cp "$DB_PATH" "${BACKUP_DIR}/vpnbzk.db.bak"
        ok "Сохранена БД → ${BACKUP_DIR}/vpnbzk.db.bak"
    else
        # Попробуем из Docker volume
        if docker compose -f "${PROJECT_DIR}/docker-compose.yml" exec ufo-app \
            sh -c "cat /project/data/vpnbzk.db" > "${BACKUP_DIR}/vpnbzk.db.bak" 2>/dev/null; then
            ok "Сохранена БД из контейнера → ${BACKUP_DIR}/vpnbzk.db.bak"
        else
            warn "БД не найдена — пропускаем. Пользователи не будут потеряны при замене NL-сервера."
        fi
    fi

    # Обновить symlink на последний бэкап
    ln -sfn "$BACKUP_DIR" "$BACKUP_LATEST_LINK"
    ok "Ссылка на последний бэкап → $BACKUP_LATEST_LINK"

    # Сохранить текущие значения из .env для сравнения
    OLD_NL_IP=$(grep "^NL_SERVER_IP=" "$ENV_FILE" | cut -d= -f2-)
    OLD_PUB_KEY=$(grep "^REALITY_PUBLIC_KEY=" "$ENV_FILE" | cut -d= -f2-)
    OLD_PRIV_KEY=$(grep "^REALITY_PRIVATE_KEY=" "$ENV_FILE" | cut -d= -f2-)
    OLD_SHORT_ID=$(grep "^REALITY_SHORT_ID=" "$ENV_FILE" | cut -d= -f2-)
    OLD_RU_TRANSIT_UUID=$(grep "^RU_TRANSIT_UUID=" "$ENV_FILE" | cut -d= -f2- 2>/dev/null || echo "")
    OLD_RU_TRANSIT_PUB=$(grep "^RU_TRANSIT_PUBLIC_KEY=" "$ENV_FILE" | cut -d= -f2- 2>/dev/null || echo "")

    cat > "${BACKUP_DIR}/old-values.env" <<EOF
# Старые значения NL-сервера (до миграции)
# Сохранено: $(date)
NL_SERVER_IP=${OLD_NL_IP}
REALITY_PUBLIC_KEY=${OLD_PUB_KEY}
REALITY_PRIVATE_KEY=${OLD_PRIV_KEY}
REALITY_SHORT_ID=${OLD_SHORT_ID}
RU_TRANSIT_UUID=${OLD_RU_TRANSIT_UUID}
RU_TRANSIT_PUBLIC_KEY=${OLD_RU_TRANSIT_PUB}
EOF
    ok "Старые значения сохранены → ${BACKUP_DIR}/old-values.env"

    echo ""
    sep
    echo ""
    echo -e "  ${BOLD}Бэкап создан:${NC} ${BACKUP_DIR}"
    echo ""
    echo -e "  ${YELLOW}Следующий шаг:${NC} Настройте НОВЫЙ NL-сервер:"
    echo -e "  ${CYAN}  bash scripts/setup-nl-server.sh${NC}  (на новом сервере)"
    echo ""
    echo -e "  Затем вернитесь сюда и запустите:"
    echo -e "  ${CYAN}  bash scripts/07-migrate-nl-server.sh --apply${NC}"
    echo ""
}

# ════════════════════════════════════════════════════════════
#  Функция: применить новые данные
# ════════════════════════════════════════════════════════════
do_apply() {
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
    echo -e "${CYAN}  Шаг 2/3: Введите данные нового NL-сервера   ${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════${NC}"
    echo ""

    # Загружаем старые значения для подсказок
    OLD_NL_IP=$(grep "^NL_SERVER_IP=" "$ENV_FILE" | cut -d= -f2- || echo "")
    OLD_TRANSIT_UUID=$(grep "^RU_TRANSIT_UUID=" "$ENV_FILE" | cut -d= -f2- 2>/dev/null || echo "")
    OLD_TRANSIT_PUB=$(grep "^RU_TRANSIT_PUBLIC_KEY=" "$ENV_FILE" | cut -d= -f2- 2>/dev/null || echo "")

    echo -e "${YELLOW}Старый NL IP: ${OLD_NL_IP:-не задан}${NC}"
    echo ""
    echo -e "Введите данные из вывода скрипта setup-nl-server.sh на НОВОМ сервере:"
    echo ""

    read -rp "Новый NL IP: " NEW_NL_IP
    [ -n "$NEW_NL_IP" ] || { err "IP не указан"; exit 1; }

    read -rp "Новый REALITY Public Key: " NEW_PUB_KEY
    [ -n "$NEW_PUB_KEY" ] || { err "Public Key не указан"; exit 1; }

    read -rp "Новый REALITY Private Key: " NEW_PRIV_KEY
    [ -n "$NEW_PRIV_KEY" ] || { err "Private Key не указан"; exit 1; }

    read -rp "Новый Short ID: " NEW_SHORT_ID
    [ -n "$NEW_SHORT_ID" ] || { err "Short ID не указан"; exit 1; }

    read -rp "Новый Transit UUID (для RU→NL каскада): " NEW_TRANSIT_UUID
    [ -n "$NEW_TRANSIT_UUID" ] || { err "Transit UUID не указан"; exit 1; }

    # Transit Public Key — это тот же REALITY Public Key нового NL-сервера
    NEW_TRANSIT_PUB="$NEW_PUB_KEY"

    read -rp "Short ID для RU транзита [${NEW_SHORT_ID}]: " NEW_TRANSIT_SHORT_ID
    NEW_TRANSIT_SHORT_ID="${NEW_TRANSIT_SHORT_ID:-$NEW_SHORT_ID}"

    echo ""
    sep
    echo ""
    echo -e "  ${BOLD}Будем применять:${NC}"
    echo -e "  NL_SERVER_IP              = ${CYAN}${NEW_NL_IP}${NC}"
    echo -e "  REALITY_PUBLIC_KEY        = ${CYAN}${NEW_PUB_KEY}${NC}"
    echo -e "  REALITY_PRIVATE_KEY       = ${CYAN}${NEW_PRIV_KEY:0:20}...${NC}"
    echo -e "  REALITY_SHORT_ID          = ${CYAN}${NEW_SHORT_ID}${NC}"
    echo -e "  RU_TRANSIT_UUID           = ${CYAN}${NEW_TRANSIT_UUID}${NC}"
    echo -e "  RU_TRANSIT_PUBLIC_KEY     = ${CYAN}${NEW_TRANSIT_PUB:0:20}...${NC}"
    echo ""
    read -rp "Применить изменения? (yes/no): " CONFIRM
    [ "$CONFIRM" = "yes" ] || { warn "Отменено"; exit 0; }

    echo ""
    log "Обновление .env..."

    # Создаём финальный бэкап .env перед изменением
    cp "$ENV_FILE" "${ENV_FILE}.before-migrate"
    ok "Бэкап .env → ${ENV_FILE}.before-migrate"

    # Функция замены или добавления переменной в .env
    set_env() {
        local key="$1"
        local val="$2"
        if grep -q "^${key}=" "$ENV_FILE"; then
            # Экранируем спецсимволы в значении для sed
            local escaped_val
            escaped_val=$(printf '%s\n' "$val" | sed 's/[[\.*^$()+?{|]/\\&/g')
            sed -i "s|^${key}=.*|${key}=${escaped_val}|" "$ENV_FILE"
        else
            echo "${key}=${val}" >> "$ENV_FILE"
        fi
    }

    set_env "NL_SERVER_IP"          "$NEW_NL_IP"
    set_env "REALITY_PUBLIC_KEY"    "$NEW_PUB_KEY"
    set_env "REALITY_PRIVATE_KEY"   "$NEW_PRIV_KEY"
    set_env "REALITY_SHORT_ID"      "$NEW_SHORT_ID"
    set_env "RU_TRANSIT_UUID"       "$NEW_TRANSIT_UUID"
    set_env "RU_TRANSIT_PUBLIC_KEY" "$NEW_TRANSIT_PUB"
    set_env "RU_TRANSIT_SHORT_ID"   "$NEW_TRANSIT_SHORT_ID"

    ok ".env обновлён"

    # Обновить конфиг RU-сервера (если IP RU-сервера известен)
    RU_SERVER_IP=$(grep "^RU_SERVER_IP=" "$ENV_FILE" | cut -d= -f2- || echo "")
    if [ -n "$RU_SERVER_IP" ]; then
        echo ""
        warn "Не забудьте обновить конфиг RU-сервера (${RU_SERVER_IP})!"
        echo -e "  Выполните на RU-сервере:"
        echo -e "  ${CYAN}ssh root@${RU_SERVER_IP}${NC}"
        cat <<RUEOF

# В /usr/local/etc/xray/config.json найдите блок "NL-PROXY" outbound
# и обновите:
#   "address": "${NEW_NL_IP}"
#   "publicKey": "${NEW_PUB_KEY}"
#   "shortId": "${NEW_TRANSIT_SHORT_ID}"
#   "id": "${NEW_TRANSIT_UUID}"
# Затем: systemctl restart xray

RUEOF
        read -rp "Хотите сгенерировать готовый патч конфига RU-сервера? (yes/no): " GEN_PATCH
        if [ "$GEN_PATCH" = "yes" ]; then
            gen_ru_patch "$NEW_NL_IP" "$NEW_PUB_KEY" "$NEW_TRANSIT_UUID" "$NEW_TRANSIT_SHORT_ID"
        fi
    fi

    echo ""
    log "Пересборка и перезапуск ufo-app..."
    cd "$PROJECT_DIR"
    docker compose build --no-cache ufo-app
    docker compose up -d --force-recreate ufo-app

    log "Ожидаем запуска (20 сек.)..."
    sleep 20

    # Проверка здоровья
    if docker compose exec ufo-app python -c "
import urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8000/', timeout=5)
    print('OK')
except Exception as e:
    print('FAIL:', e)
" 2>/dev/null | grep -q "OK"; then
        ok "Приложение запустилось"
    else
        warn "Приложение не ответило — проверьте логи: docker compose logs ufo-app"
    fi

    log "Синхронизация Xray конфига..."
    docker compose exec ufo-app python -c "
from app.models import SessionLocal
from app.xray import sync_and_reload
db = SessionLocal()
result = sync_and_reload(db)
db.close()
print('Xray sync:', 'OK' if result else 'FAIL')
" 2>/dev/null || warn "Не удалось синхронизировать Xray автоматически — сделайте это через /admin → Система → Синх. Xray"

    echo ""
    sep
    echo ""
    echo -e "  ${GREEN}${BOLD}Миграция завершена!${NC}"
    echo ""
    echo -e "  ${BOLD}Что проверить:${NC}"
    echo -e "  1. Открой сайт — должен открываться нормально"
    echo -e "  2. Подключись к VPN и проверь ip через ifconfig.me → должен быть ${NEW_NL_IP}"
    echo -e "  3. Зайди на yandex.ru — IP должен быть российским"
    echo -e "  4. Обнови подписку в клиентских приложениях (или клиенты обновятся сами)"
    echo ""
    echo -e "  ${BOLD}Если что-то пошло не так:${NC}"
    echo -e "  ${CYAN}  bash scripts/07-migrate-nl-server.sh --rollback${NC}"
    echo ""
    echo -e "  ${BOLD}Старые данные сохранены в:${NC}"
    echo -e "  ${ENV_FILE}.before-migrate"
    echo ""
}

# ════════════════════════════════════════════════════════════
#  Функция: откат к старым данным
# ════════════════════════════════════════════════════════════
do_rollback() {
    echo ""
    echo -e "${RED}═══════════════════════════════════════════════${NC}"
    echo -e "${RED}  ОТКАТ к предыдущим настройкам NL-сервера    ${NC}"
    echo -e "${RED}═══════════════════════════════════════════════${NC}"
    echo ""

    if [ -f "${ENV_FILE}.before-migrate" ]; then
        echo -e "Найден бэкап: ${ENV_FILE}.before-migrate"
        read -rp "Восстановить .env из бэкапа? (yes/no): " CONFIRM
        [ "$CONFIRM" = "yes" ] || { warn "Отменено"; exit 0; }

        cp "$ENV_FILE" "${ENV_FILE}.rollback-$(date +%H%M%S)"
        cp "${ENV_FILE}.before-migrate" "$ENV_FILE"
        ok ".env восстановлён"

        log "Перезапуск ufo-app..."
        cd "$PROJECT_DIR"
        docker compose build --no-cache ufo-app
        docker compose up -d --force-recreate ufo-app
        ok "Приложение перезапущено с прежними настройками"
    elif [ -L "$BACKUP_LATEST_LINK" ]; then
        LATEST=$(readlink -f "$BACKUP_LATEST_LINK")
        echo -e "Бэкап найден: ${LATEST}/.env.bak"
        read -rp "Восстановить из него? (yes/no): " CONFIRM
        [ "$CONFIRM" = "yes" ] || { warn "Отменено"; exit 0; }

        cp "$ENV_FILE" "${ENV_FILE}.rollback-$(date +%H%M%S)"
        cp "${LATEST}/.env.bak" "$ENV_FILE"
        ok ".env восстановлён из резервной копии"

        cd "$PROJECT_DIR"
        docker compose build --no-cache ufo-app
        docker compose up -d --force-recreate ufo-app
        ok "Приложение перезапущено"
    else
        err "Бэкап не найден. Восстановите вручную из ${ENV_FILE}.before-migrate или из папки backups/"
        exit 1
    fi

    echo ""
    ok "Откат завершён. Проверьте работу сайта."
    echo ""
}

# ════════════════════════════════════════════════════════════
#  Генерация патча для RU сервера
# ════════════════════════════════════════════════════════════
gen_ru_patch() {
    local NL_IP="$1"
    local NL_PUB="$2"
    local TRANSIT_UUID="$3"
    local TRANSIT_SID="$4"

    PATCH_FILE="${PROJECT_DIR}/backups/ru-server-patch-$(date +%Y%m%d-%H%M%S).sh"
    mkdir -p "${PROJECT_DIR}/backups"

    cat > "$PATCH_FILE" <<PATCHEOF
#!/usr/bin/env bash
# Патч для RU-сервера: обновить endpoint NL-сервера
# Сгенерировано: $(date)
# Скопируйте этот файл на RU-сервер и запустите как root

set -euo pipefail

CONFIG="/usr/local/etc/xray/config.json"

# Бэкап
cp "\$CONFIG" "\${CONFIG}.bak.$(date +%Y%m%d%H%M%S)"

# Обновление через python (jq может не быть установлен)
python3 - <<'PYEOF'
import json, sys

cfg = json.load(open('${CONFIG}'))

updated = False
for ob in cfg.get('outbounds', []):
    if ob.get('tag') == 'NL-PROXY':
        vnext = ob.get('settings', {}).get('vnext', [{}])
        if vnext:
            vnext[0]['address'] = '${NL_IP}'
            for user in vnext[0].get('users', []):
                user['id'] = '${TRANSIT_UUID}'
        ss = ob.get('streamSettings', {})
        rs = ss.get('realitySettings', {})
        rs['publicKey'] = '${NL_PUB}'
        rs['shortId'] = '${TRANSIT_SID}'
        updated = True

if not updated:
    print('ERROR: NL-PROXY outbound not found in config!', file=sys.stderr)
    sys.exit(1)

json.dump(cfg, open('/usr/local/etc/xray/config.json', 'w'), indent=2, ensure_ascii=False)
print('Config updated successfully')
PYEOF

systemctl restart xray
echo "Xray restarted"
systemctl is-active xray && echo "Xray is running OK" || echo "ERROR: Xray failed to start"
PATCHEOF

    chmod +x "$PATCH_FILE"
    ok "Патч для RU-сервера сохранён → ${PATCH_FILE}"
    echo ""
    echo -e "  Скопируйте и запустите на RU-сервере:"
    echo -e "  ${CYAN}scp ${PATCH_FILE} root@${RU_SERVER_IP}:/tmp/ru-patch.sh${NC}"
    echo -e "  ${CYAN}ssh root@${RU_SERVER_IP} 'bash /tmp/ru-patch.sh'${NC}"
}

# ════════════════════════════════════════════════════════════
#  Главное меню
# ════════════════════════════════════════════════════════════

case "$MODE" in
    --backup)
        do_backup
        ;;
    --apply)
        # Проверяем наличие бэкапа
        if [ ! -f "${ENV_FILE}.before-migrate" ] && [ ! -L "$BACKUP_LATEST_LINK" ]; then
            warn "Бэкап не найден. Создаём автоматически..."
            do_backup
        fi
        do_apply
        ;;
    --rollback)
        do_rollback
        ;;
    "")
        # Интерактивный режим
        echo ""
        echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
        echo -e "${CYAN}  Замена NL-сервера — интерактивный режим                 ${NC}"
        echo -e "${CYAN}═══════════════════════════════════════════════════════════${NC}"
        echo ""
        echo -e "  ${BOLD}Что вы хотите сделать?${NC}"
        echo ""
        echo -e "  ${CYAN}1)${NC} Полная миграция (бэкап + ввод новых данных + применение)"
        echo -e "  ${CYAN}2)${NC} Только создать бэкап"
        echo -e "  ${CYAN}3)${NC} Только применить новые данные (бэкап уже есть)"
        echo -e "  ${CYAN}4)${NC} Откатиться к предыдущим настройкам"
        echo ""
        read -rp "Выбор [1-4]: " CHOICE

        case "$CHOICE" in
            1) do_backup; do_apply ;;
            2) do_backup ;;
            3) do_apply ;;
            4) do_rollback ;;
            *) err "Неверный выбор"; exit 1 ;;
        esac
        ;;
    *)
        err "Неизвестный флаг: $MODE"
        echo "Использование: $0 [--backup|--apply|--rollback]"
        exit 1
        ;;
esac
