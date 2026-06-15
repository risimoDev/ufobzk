#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  12-switch-ru-cascade.sh — Быстрое переключение каскадного RU-сервера
# ═══════════════════════════════════════════════════════════════════════════
#
#  Запускается НА ОСНОВНОМ (NL) сервере. Меняет каскадный RU-exit без
#  обрушения проекта: обновляет .env, пересоздаёт ТОЛЬКО app-контейнер
#  (xray перечитывает новый RU-outbound), форсит пересинхронизацию нод.
#
#  Простой при переключении — единственный краткий рестарт xray на NL
#  (несколько секунд). Делайте в час низкой нагрузки.
#
#  ПОДГОТОВКА (сначала на НОВОМ RU-сервере):
#    1. Возьмите NL REALITY Public Key и Transit UUID (см. .env основного:
#       REALITY_PUBLIC_KEY и RU_TRANSIT_UUID).
#    2. Запустите на новом RU: bash scripts/setup-ru-server.sh
#       — он спросит NL Public Key + Transit UUID, сгенерирует СВОИ
#         RU REALITY Public Key + Short ID и напечатает их в конце.
#    3. Эти значения передайте этому скрипту (см. ниже).
#
#  ИСПОЛЬЗОВАНИЕ:
#    bash scripts/12-switch-ru-cascade.sh \
#        --ip 195.x.x.x \
#        --pubkey <RU_REALITY_PUBLIC_KEY> \
#        --shortid <RU_SHORT_ID> \
#        --sn www.samsung.com \
#        [--port 443] [--uuid <TRANSIT_UUID>] [--no-deploy]
#
#  Если флаги не заданы — скрипт спросит интерактивно.
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
log()  { echo -e "${CYAN}[•]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

# ── Определяем корень проекта (скрипт лежит в scripts/) ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${PROJECT_DIR}/.env"

NEW_IP=""; NEW_PUBKEY=""; NEW_SHORTID=""; NEW_SN=""; NEW_PORT="443"; NEW_UUID=""; DO_DEPLOY=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ip)       NEW_IP="$2"; shift 2 ;;
        --pubkey)   NEW_PUBKEY="$2"; shift 2 ;;
        --shortid)  NEW_SHORTID="$2"; shift 2 ;;
        --sn)       NEW_SN="$2"; shift 2 ;;
        --port)     NEW_PORT="$2"; shift 2 ;;
        --uuid)     NEW_UUID="$2"; shift 2 ;;
        --no-deploy) DO_DEPLOY=false; shift ;;
        *) err "Неизвестный флаг: $1"; exit 1 ;;
    esac
done

[ -f "$ENV_FILE" ] || { err ".env не найден: $ENV_FILE — запустите на основном сервере"; exit 1; }

# ── Текущие значения для справки ──
cur() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- || true; }
echo -e "${CYAN}═══ Текущий каскадный RU ═══${NC}"
echo "  RU_SERVER_IP          = $(cur RU_SERVER_IP)"
echo "  RU_TRANSIT_PORT       = $(cur RU_TRANSIT_PORT)"
echo "  RU_TRANSIT_PUBLIC_KEY = $(cur RU_TRANSIT_PUBLIC_KEY)"
echo "  RU_TRANSIT_SHORT_ID   = $(cur RU_TRANSIT_SHORT_ID)"
echo "  RU_TRANSIT_SN         = $(cur RU_TRANSIT_SN)"
echo "  RU_TRANSIT_UUID       = $(cur RU_TRANSIT_UUID)"
echo ""

# ── Интерактивный ввод недостающего ──
[ -n "$NEW_IP" ]      || read -rp "Новый RU IP: " NEW_IP
[ -n "$NEW_PUBKEY" ]  || read -rp "Новый RU REALITY Public Key: " NEW_PUBKEY
[ -n "$NEW_SHORTID" ] || read -rp "Новый RU Short ID: " NEW_SHORTID
[ -n "$NEW_SN" ]      || read -rp "Новый RU serverName/SNI [www.samsung.com]: " NEW_SN
NEW_SN="${NEW_SN:-www.samsung.com}"

[ -n "$NEW_IP" ] && [ -n "$NEW_PUBKEY" ] && [ -n "$NEW_SHORTID" ] || { err "IP, Public Key и Short ID обязательны"; exit 1; }

# ── Pre-check: доступен ли новый RU с этого сервера ──
log "Проверка доступности ${NEW_IP}:${NEW_PORT} с NL-сервера..."
if timeout 5 bash -c "echo > /dev/tcp/${NEW_IP}/${NEW_PORT}" 2>/dev/null; then
    ok "Порт ${NEW_PORT} на ${NEW_IP} открыт"
else
    warn "Не удалось подключиться к ${NEW_IP}:${NEW_PORT}!"
    warn "Убедитесь, что на новом RU поднят xray (REALITY на ${NEW_PORT}) и UFW пускает NL."
    read -rp "Всё равно продолжить? (y/N): " yn
    [ "$yn" = "y" ] || { err "Отменено"; exit 1; }
fi

# ── Бэкап .env ──
BACKUP="${ENV_FILE}.bak.$(date +%Y%m%d-%H%M%S)"
cp "$ENV_FILE" "$BACKUP"
ok "Бэкап .env → $BACKUP"

# ── upsert KEY=VALUE в .env (idempotent) ──
upsert() {
    local key="$1" val="$2"
    if grep -qE "^${key}=" "$ENV_FILE"; then
        # экранируем для sed (val может содержать / и спецсимволы — используем |)
        local esc; esc=$(printf '%s' "$val" | sed -e 's/[\\&|]/\\&/g')
        sed -i -E "s|^${key}=.*|${key}=${esc}|" "$ENV_FILE"
    else
        printf '%s=%s\n' "$key" "$val" >> "$ENV_FILE"
    fi
}

log "Обновление .env..."
upsert RU_SERVER_IP          "$NEW_IP"
upsert RU_TRANSIT_PORT       "$NEW_PORT"
upsert RU_TRANSIT_PUBLIC_KEY "$NEW_PUBKEY"
upsert RU_TRANSIT_SHORT_ID   "$NEW_SHORTID"
upsert RU_TRANSIT_SN         "$NEW_SN"
[ -n "$NEW_UUID" ] && upsert RU_TRANSIT_UUID "$NEW_UUID"
ok ".env обновлён"

if [ "$DO_DEPLOY" = false ]; then
    warn "--no-deploy: контейнер не пересоздан. Примените вручную:"
    echo "  cd $PROJECT_DIR && docker compose up -d ufo-app"
    exit 0
fi

# ── Пересоздаём ТОЛЬКО app-контейнер (подхватит новый .env) ──
# ufo-app при старте пересоберёт xray config.json с новым RU-outbound и
# рестартанёт xray один раз. nginx и остальное не трогаем.
log "Пересоздание ufo-app (подхват нового .env)..."
cd "$PROJECT_DIR"
docker compose up -d ufo-app
ok "ufo-app пересоздан"

# ── Проверка: новый IP попал в конфиг xray ──
log "Ожидание применения конфига (10с)..."
sleep 10
if docker compose exec -T ufo-app sh -c "grep -q '$NEW_IP' /etc/xray/config.json" 2>/dev/null; then
    ok "RU-PROXY в xray config указывает на $NEW_IP"
else
    warn "Не нашёл $NEW_IP в /etc/xray/config.json — проверьте логи: docker compose logs ufo-app"
fi

echo ""
echo -e "${GREEN}${BOLD}Каскадный RU переключён на ${NEW_IP}${NC}"
echo ""
echo -e "  ${BOLD}Дальше:${NC}"
echo -e "  1. Remote-ноды подхватят новый RU при ближайшей синхронизации (≤5 мин)."
echo -e "  2. Проверьте маршрут: на клиенте откройте 2ip.ru / whoer.net через .ru-сайт —"
echo -e "     выходной IP для российских доменов должен стать ${NEW_IP}."
echo -e "  3. Убедившись, что всё работает, гасите старый RU."
echo -e "  ${YELLOW}Откат:${NC} cp ${BACKUP} ${ENV_FILE} && docker compose up -d ufo-app"
echo ""
