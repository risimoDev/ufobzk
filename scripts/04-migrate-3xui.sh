#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  04-migrate-3xui.sh — Миграция пользователей 3X-UI → Marzban
# ═══════════════════════════════════════════════════════════
#  Что делает:
#   • Парсит БД 3X-UI (SQLite) и извлекает клиентов
#   • Создаёт пользователей в Marzban через REST API
#   • Сохраняет UUID клиентов (подключения не сломаются)
#   • Переносит лимиты трафика и сроки действия
#   • Бэкапит всё перед началом
#
#  Требования:
#   • Marzban уже запущен и работает (02-install.sh)
#   • БД 3X-UI доступна (обычно /etc/x-ui/x-ui.db)
#   • jq, sqlite3
#
#  Запуск: sudo bash scripts/04-migrate-3xui.sh [путь_к_db]
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

# ══════════════════════════════════════════
# 0. Проверки
# ══════════════════════════════════════════

echo ""
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo -e "${CYAN}  Миграция 3X-UI → Marzban                ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo ""

# jq
if ! command -v jq &>/dev/null; then
    log "Установка jq..."
    apt-get update -qq && apt-get install -y -qq jq
    ok "jq установлен"
fi

# sqlite3
if ! command -v sqlite3 &>/dev/null; then
    log "Установка sqlite3..."
    apt-get update -qq && apt-get install -y -qq sqlite3
    ok "sqlite3 установлен"
fi

# curl
if ! command -v curl &>/dev/null; then
    err "curl не найден"
    exit 1
fi

# ── Путь к БД 3X-UI ──
XUIDB="${1:-/etc/x-ui/x-ui.db}"

if [ ! -f "$XUIDB" ]; then
    err "БД 3X-UI не найдена: $XUIDB"
    echo ""
    echo "  Использование: sudo bash $0 [путь_к_x-ui.db]"
    echo "  Стандартный путь: /etc/x-ui/x-ui.db"
    echo ""
    exit 1
fi

ok "Найдена БД 3X-UI: $XUIDB ($(du -sh "$XUIDB" | cut -f1))"

# ── Проверяем .env и Marzban ──

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

if [ -f "$PROJECT_DIR/.env" ]; then
    source "$PROJECT_DIR/.env"
elif [ -f "/opt/vpnbzk/.env" ]; then
    source /opt/vpnbzk/.env
    PROJECT_DIR="/opt/vpnbzk"
else
    err ".env не найден. Сначала: bash scripts/02-install.sh"
    exit 1
fi

MARZBAN_URL="http://localhost:${MARZBAN_PORT:-8880}"
MARZBAN_USER="${MARZBAN_ADMIN_USER:-admin}"
MARZBAN_PASS="${MARZBAN_ADMIN_PASS:-}"

if [ -z "$MARZBAN_PASS" ]; then
    err "MARZBAN_ADMIN_PASS не задан в .env"
    exit 1
fi

# Проверяем соединение
log "Проверка связи с Marzban..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$MARZBAN_URL/api/admin/token" \
    -X POST -d "username=$MARZBAN_USER&password=$MARZBAN_PASS" \
    -H "Content-Type: application/x-www-form-urlencoded" 2>/dev/null || echo "000")

if [ "$HTTP_CODE" = "000" ]; then
    err "Marzban не отвечает ($MARZBAN_URL)"
    echo "  Убедитесь, что контейнер marzban запущен:"
    echo "  docker compose ps"
    exit 1
fi

# Получаем токен
log "Авторизация в Marzban..."
TOKEN_RESP=$(curl -s "$MARZBAN_URL/api/admin/token" \
    -X POST -d "username=$MARZBAN_USER&password=$MARZBAN_PASS" \
    -H "Content-Type: application/x-www-form-urlencoded")

MARZBAN_TOKEN=$(echo "$TOKEN_RESP" | jq -r '.access_token // empty')

if [ -z "$MARZBAN_TOKEN" ]; then
    err "Не удалось получить токен Marzban"
    echo "  Ответ: $TOKEN_RESP"
    exit 1
fi

ok "Авторизация успешна"

# Получаем список inbound'ов Marzban
log "Получение inbound'ов Marzban..."
INBOUNDS_JSON=$(curl -s "$MARZBAN_URL/api/inbounds" \
    -H "Authorization: Bearer $MARZBAN_TOKEN")

# Определяем доступные протоколы
MARZBAN_PROTOCOLS=$(echo "$INBOUNDS_JSON" | jq -r 'keys[]' 2>/dev/null || echo "")
ok "Доступные протоколы: $MARZBAN_PROTOCOLS"

# ══════════════════════════════════════════
# 1. Бэкап
# ══════════════════════════════════════════

BACKUP_DIR="$PROJECT_DIR/backups/migration_$(date '+%Y%m%d_%H%M%S')"
mkdir -p "$BACKUP_DIR"

cp "$XUIDB" "$BACKUP_DIR/x-ui.db"
ok "Бэкап 3X-UI: $BACKUP_DIR/x-ui.db"

# ══════════════════════════════════════════
# 2. Парсинг 3X-UI
# ══════════════════════════════════════════

log "Чтение inbound'ов 3X-UI..."

# Структура 3X-UI: таблица inbounds с JSON-полем settings содержащим clients
INBOUND_COUNT=$(sqlite3 "$XUIDB" "SELECT COUNT(*) FROM inbounds;" 2>/dev/null || echo "0")

if [ "$INBOUND_COUNT" = "0" ]; then
    err "В БД 3X-UI не найдено inbound'ов"
    exit 1
fi

ok "Найдено inbound'ов: $INBOUND_COUNT"

# Извлекаем все inbound'ы
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

sqlite3 -json "$XUIDB" "SELECT id, remark, protocol, settings, stream_settings, port, enable, up, down, total, expiry_time FROM inbounds;" \
    > "$TMPDIR/inbounds.json" 2>/dev/null

if [ ! -s "$TMPDIR/inbounds.json" ]; then
    # Fallback: sqlite3 без -json (старые версии)
    warn "sqlite3 -json не поддерживается, используем альтернативный метод..."

    sqlite3 "$XUIDB" ".mode csv
.headers on
SELECT id, remark, protocol, settings, port, enable, up, down, total, expiry_time FROM inbounds;" \
        > "$TMPDIR/inbounds.csv"

    # Для каждого inbound извлекаем clients из settings JSON
    > "$TMPDIR/clients.json"
    echo "[" > "$TMPDIR/all_clients.json"
    FIRST=true

    IDS=$(sqlite3 "$XUIDB" "SELECT id FROM inbounds;")
    for IB_ID in $IDS; do
        PROTOCOL=$(sqlite3 "$XUIDB" "SELECT protocol FROM inbounds WHERE id=$IB_ID;")
        REMARK=$(sqlite3 "$XUIDB" "SELECT remark FROM inbounds WHERE id=$IB_ID;")
        SETTINGS=$(sqlite3 "$XUIDB" "SELECT settings FROM inbounds WHERE id=$IB_ID;")
        PORT=$(sqlite3 "$XUIDB" "SELECT port FROM inbounds WHERE id=$IB_ID;")
        TOTAL=$(sqlite3 "$XUIDB" "SELECT total FROM inbounds WHERE id=$IB_ID;")
        EXPIRY=$(sqlite3 "$XUIDB" "SELECT expiry_time FROM inbounds WHERE id=$IB_ID;")
        UP=$(sqlite3 "$XUIDB" "SELECT up FROM inbounds WHERE id=$IB_ID;")
        DOWN=$(sqlite3 "$XUIDB" "SELECT down FROM inbounds WHERE id=$IB_ID;")

        # Парсим клиентов из settings JSON
        CLIENTS=$(echo "$SETTINGS" | jq -c '.clients[]?' 2>/dev/null || echo "")

        if [ -z "$CLIENTS" ]; then
            warn "Inbound #${IB_ID} (${REMARK}): нет клиентов"
            continue
        fi

        echo "$CLIENTS" | while IFS= read -r CLIENT; do
            # Получаем идентификатор (id для vless/vmess, password для trojan)
            if [ "$PROTOCOL" = "trojan" ]; then
                CLIENT_ID=$(echo "$CLIENT" | jq -r '.password // empty')
            else
                CLIENT_ID=$(echo "$CLIENT" | jq -r '.id // empty')
            fi
            EMAIL=$(echo "$CLIENT" | jq -r '.email // empty')
            FLOW=$(echo "$CLIENT" | jq -r '.flow // empty')
            TOTAL_GB=$(echo "$CLIENT" | jq -r '.totalGB // 0')
            EXPIRY_MS=$(echo "$CLIENT" | jq -r '.expiryTime // 0')

            # Если .totalGB == 0, используем лимит inbound
            if [ "$TOTAL_GB" = "0" ] && [ "$TOTAL" != "0" ]; then
                DATA_LIMIT="$TOTAL"
            elif [ "$TOTAL_GB" != "0" ]; then
                # totalGB в ГБ → байты
                DATA_LIMIT=$(echo "$TOTAL_GB * 1073741824" | bc 2>/dev/null || echo "0")
            else
                DATA_LIMIT="0"
            fi

            # Expiry: клиентский или inbound'ный (мс → unix timestamp)
            if [ "$EXPIRY_MS" = "0" ] && [ "$EXPIRY" != "0" ]; then
                EXPIRE_TS=$EXPIRY
            elif [ "$EXPIRY_MS" != "0" ]; then
                EXPIRE_TS=$(echo "$EXPIRY_MS / 1000" | bc 2>/dev/null || echo "$EXPIRY_MS")
            else
                EXPIRE_TS="0"
            fi

            echo "${PROTOCOL}|${CLIENT_ID}|${EMAIL}|${FLOW}|${DATA_LIMIT}|${EXPIRE_TS}|${UP}|${DOWN}" \
                >> "$TMPDIR/clients_list.txt"
        done
    done
else
    # JSON mode — парсим через jq
    > "$TMPDIR/clients_list.txt"

    jq -c '.[]' "$TMPDIR/inbounds.json" | while IFS= read -r INBOUND; do
        PROTOCOL=$(echo "$INBOUND" | jq -r '.protocol')
        REMARK=$(echo "$INBOUND" | jq -r '.remark')
        SETTINGS=$(echo "$INBOUND" | jq -r '.settings')
        TOTAL=$(echo "$INBOUND" | jq -r '.total // 0')
        EXPIRY=$(echo "$INBOUND" | jq -r '.expiry_time // 0')
        UP=$(echo "$INBOUND" | jq -r '.up // 0')
        DOWN=$(echo "$INBOUND" | jq -r '.down // 0')

        CLIENTS=$(echo "$SETTINGS" | jq -c '.clients[]?' 2>/dev/null || echo "")

        if [ -z "$CLIENTS" ]; then
            warn "Inbound (${REMARK}): нет клиентов"
            continue
        fi

        echo "$CLIENTS" | while IFS= read -r CLIENT; do
            if [ "$PROTOCOL" = "trojan" ]; then
                CLIENT_ID=$(echo "$CLIENT" | jq -r '.password // empty')
            else
                CLIENT_ID=$(echo "$CLIENT" | jq -r '.id // empty')
            fi
            EMAIL=$(echo "$CLIENT" | jq -r '.email // empty')
            FLOW=$(echo "$CLIENT" | jq -r '.flow // empty')
            TOTAL_GB=$(echo "$CLIENT" | jq -r '.totalGB // 0')
            EXPIRY_MS=$(echo "$CLIENT" | jq -r '.expiryTime // 0')

            if [ "$TOTAL_GB" = "0" ] && [ "$TOTAL" != "0" ]; then
                DATA_LIMIT="$TOTAL"
            elif [ "$TOTAL_GB" != "0" ]; then
                DATA_LIMIT=$(echo "$TOTAL_GB * 1073741824" | bc 2>/dev/null || echo "0")
            else
                DATA_LIMIT="0"
            fi

            if [ "$EXPIRY_MS" = "0" ] && [ "$EXPIRY" != "0" ]; then
                EXPIRE_TS=$EXPIRY
            elif [ "$EXPIRY_MS" != "0" ]; then
                EXPIRE_TS=$(echo "$EXPIRY_MS / 1000" | bc 2>/dev/null || echo "$EXPIRY_MS")
            else
                EXPIRE_TS="0"
            fi

            echo "${PROTOCOL}|${CLIENT_ID}|${EMAIL}|${FLOW}|${DATA_LIMIT}|${EXPIRE_TS}|${UP}|${DOWN}" \
                >> "$TMPDIR/clients_list.txt"
        done
    done
fi

# ══════════════════════════════════════════
# 3. Подсчёт и подтверждение
# ══════════════════════════════════════════

if [ ! -s "$TMPDIR/clients_list.txt" ]; then
    err "Не найдено клиентов для миграции"
    exit 1
fi

TOTAL_CLIENTS=$(wc -l < "$TMPDIR/clients_list.txt")
VLESS_COUNT=$(grep -c '^vless|' "$TMPDIR/clients_list.txt" || echo "0")
VMESS_COUNT=$(grep -c '^vmess|' "$TMPDIR/clients_list.txt" || echo "0")
TROJAN_COUNT=$(grep -c '^trojan|' "$TMPDIR/clients_list.txt" || echo "0")

echo ""
echo -e "${BOLD}  Найдено клиентов: ${TOTAL_CLIENTS}${NC}"
[ "$VLESS_COUNT" != "0" ]  && echo -e "    VLESS:  $VLESS_COUNT"
[ "$VMESS_COUNT" != "0" ]  && echo -e "    VMess:  $VMESS_COUNT"
[ "$TROJAN_COUNT" != "0" ] && echo -e "    Trojan: $TROJAN_COUNT"
echo ""

# Сохраняем список для отчёта
cp "$TMPDIR/clients_list.txt" "$BACKUP_DIR/clients_list.txt"

echo -e "  Список клиентов сохранён: ${BACKUP_DIR}/clients_list.txt"
echo ""

read -rp "$(echo -e "${YELLOW}Начать миграцию ${TOTAL_CLIENTS} клиентов? [y/N]: ${NC}")" CONFIRM
if [[ "${CONFIRM,,}" != "y" ]]; then
    echo "Отменено."
    exit 0
fi

# ══════════════════════════════════════════
# 4. Создание пользователей в Marzban
# ══════════════════════════════════════════

log "Начало миграции..."

MIGRATED=0
SKIPPED=0
FAILED=0

# Лог миграции
MIGRATION_LOG="$BACKUP_DIR/migration.log"
echo "# Миграция 3X-UI → Marzban — $(date)" > "$MIGRATION_LOG"

while IFS='|' read -r PROTOCOL CLIENT_ID EMAIL FLOW DATA_LIMIT EXPIRE_TS UP DOWN; do
    # Формируем username
    # Если email есть — используем как username, иначе генерируем из UUID
    if [ -n "$EMAIL" ] && [ "$EMAIL" != "null" ]; then
        USERNAME=$(echo "$EMAIL" | sed 's/@.*//; s/[^a-zA-Z0-9_-]/_/g' | head -c 32)
    else
        USERNAME="user_$(echo "$CLIENT_ID" | cut -c1-8)"
    fi

    # Проверяем, не существует ли уже
    CHECK=$(curl -s -o /dev/null -w "%{http_code}" \
        "$MARZBAN_URL/api/user/$USERNAME" \
        -H "Authorization: Bearer $MARZBAN_TOKEN" 2>/dev/null || echo "000")

    if [ "$CHECK" = "200" ]; then
        warn "Пропущен (существует): $USERNAME"
        echo "SKIP|$USERNAME|$PROTOCOL|$CLIENT_ID|already_exists" >> "$MIGRATION_LOG"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi

    # Маппинг протокола
    PROXIES="{}"
    case "$PROTOCOL" in
        vless)
            if [ -n "$FLOW" ] && [ "$FLOW" != "null" ] && [ "$FLOW" != "" ]; then
                PROXIES="{\"vless\": {\"id\": \"$CLIENT_ID\", \"flow\": \"$FLOW\"}}"
            else
                PROXIES="{\"vless\": {\"id\": \"$CLIENT_ID\"}}"
            fi
            ;;
        vmess)
            PROXIES="{\"vmess\": {\"id\": \"$CLIENT_ID\"}}"
            ;;
        trojan)
            PROXIES="{\"trojan\": {\"password\": \"$CLIENT_ID\"}}"
            ;;
        *)
            warn "Неизвестный протокол: $PROTOCOL ($USERNAME)"
            echo "FAIL|$USERNAME|$PROTOCOL|$CLIENT_ID|unknown_protocol" >> "$MIGRATION_LOG"
            FAILED=$((FAILED + 1))
            continue
            ;;
    esac

    # Формируем expire (Marzban принимает unix timestamp в секундах или null)
    if [ "$EXPIRE_TS" = "0" ] || [ -z "$EXPIRE_TS" ]; then
        EXPIRE_JSON="null"
    else
        EXPIRE_JSON="$EXPIRE_TS"
    fi

    # data_limit: 0 → null (безлимитный)
    if [ "$DATA_LIMIT" = "0" ] || [ -z "$DATA_LIMIT" ]; then
        DATA_LIMIT_JSON="null"
    else
        DATA_LIMIT_JSON="$DATA_LIMIT"
    fi

    # Создаём пользователя
    PAYLOAD=$(cat <<ENDJSON
{
    "username": "$USERNAME",
    "proxies": $PROXIES,
    "data_limit": $DATA_LIMIT_JSON,
    "expire": $EXPIRE_JSON,
    "data_limit_reset_strategy": "no_reset",
    "status": "active",
    "note": "Мигрирован из 3X-UI"
}
ENDJSON
)

    RESPONSE=$(curl -s -w "\n%{http_code}" \
        "$MARZBAN_URL/api/user" \
        -X POST \
        -H "Authorization: Bearer $MARZBAN_TOKEN" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" 2>/dev/null)

    HTTP_CODE=$(echo "$RESPONSE" | tail -1)
    BODY=$(echo "$RESPONSE" | sed '$d')

    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
        ok "Создан: $USERNAME ($PROTOCOL, UUID сохранён)"
        echo "OK|$USERNAME|$PROTOCOL|$CLIENT_ID" >> "$MIGRATION_LOG"
        MIGRATED=$((MIGRATED + 1))
    elif [ "$HTTP_CODE" = "409" ]; then
        warn "Пропущен (конфликт): $USERNAME"
        echo "SKIP|$USERNAME|$PROTOCOL|$CLIENT_ID|conflict" >> "$MIGRATION_LOG"
        SKIPPED=$((SKIPPED + 1))
    else
        err "Ошибка ($HTTP_CODE): $USERNAME"
        echo "  Ответ: $BODY"
        echo "FAIL|$USERNAME|$PROTOCOL|$CLIENT_ID|http_$HTTP_CODE|$BODY" >> "$MIGRATION_LOG"
        FAILED=$((FAILED + 1))
    fi

    # Небольшая пауза чтобы не перегрузить
    sleep 0.2

done < "$TMPDIR/clients_list.txt"

# ══════════════════════════════════════════
# 5. Итог
# ══════════════════════════════════════════

echo ""
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo -e "${CYAN}  Результаты миграции                      ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo ""
echo -e "  Всего клиентов: ${BOLD}${TOTAL_CLIENTS}${NC}"
echo -e "  ${GREEN}Создано:${NC}    ${MIGRATED}"
echo -e "  ${YELLOW}Пропущено:${NC}  ${SKIPPED}"
echo -e "  ${RED}Ошибки:${NC}     ${FAILED}"
echo ""
echo -e "  Бэкап:     ${BACKUP_DIR}"
echo -e "  Лог:       ${MIGRATION_LOG}"
echo ""

if [ "$FAILED" -gt 0 ]; then
    warn "Есть ошибки! Проверьте лог: cat $MIGRATION_LOG"
    echo ""
fi

if [ "$MIGRATED" -gt 0 ]; then
    echo -e "${GREEN}  UUID/пароли клиентов сохранены —${NC}"
    echo -e "${GREEN}  существующие подключения продолжат работать!${NC}"
    echo ""
fi

echo -e "  ${BOLD}Следующие шаги:${NC}"
echo "  1. Проверьте пользователей в Marzban: $MARZBAN_URL/dashboard"
echo "  2. Убедитесь, что подключения работают"
echo "  3. Остановите 3X-UI: systemctl stop x-ui"
echo "  4. Отключите 3X-UI: systemctl disable x-ui"
echo ""
