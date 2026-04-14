#!/usr/bin/env bash
# ─────────────────────────────────────────────────
# 05-setup-reality.sh — Генерация ключей REALITY
# и автоматическое обновление конфигурации Xray
# ─────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
CONTAINER_NAME="ufobzk-xray"

# ─── Проверки ────────────────────────────────────
[ -f "$ENV_FILE" ] || error ".env не найден: $ENV_FILE"

# ─── Проверяем что контейнер Xray запущен ─────
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    warn "Контейнер $CONTAINER_NAME не запущен. Попробуем запустить..."
    cd "$PROJECT_DIR"
    docker compose up -d xray
    sleep 3
    docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$" \
        || error "Не удалось запустить $CONTAINER_NAME"
fi

# ─── Генерация x25519 ключей ────────────────────
info "Генерация пары ключей x25519 через Xray..."
KEY_OUTPUT=$(docker exec "$CONTAINER_NAME" xray x25519 2>/dev/null) \
    || error "Не удалось выполнить xray x25519 в контейнере"

PRIVATE_KEY=$(echo "$KEY_OUTPUT" | grep -i "private" | awk '{print $NF}')
PUBLIC_KEY=$(echo "$KEY_OUTPUT" | grep -i "public" | awk '{print $NF}')

[ -n "$PRIVATE_KEY" ] || error "Не удалось извлечь Private Key"
[ -n "$PUBLIC_KEY" ]  || error "Не удалось извлечь Public Key"

info "Private Key: $PRIVATE_KEY"
info "Public Key:  $PUBLIC_KEY"

# ─── Генерация Short ID ─────────────────────────
SHORT_ID=$(openssl rand -hex 8)
info "Short ID: $SHORT_ID"

# ─── Записываем ключи в .env ────────────────────
update_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

update_env "REALITY_PRIVATE_KEY" "$PRIVATE_KEY"
update_env "REALITY_PUBLIC_KEY" "$PUBLIC_KEY"
update_env "REALITY_SHORT_ID" "$SHORT_ID"

info "Ключи записаны в .env"

# ─── Перезапуск стека для применения ─────────────
info "Перезапуск приложения для применения новых ключей..."
cd "$PROJECT_DIR"
docker compose restart ufo-app
sleep 3

# ufo-app при старте вызывает sync_and_reload() — перезаписывает конфиг Xray
info "Перезапуск Xray..."
docker compose restart xray
sleep 2

docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$" \
    || error "Xray не запустился после перезапуска!"

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN} REALITY настроен успешно!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo "  Протокол:    VLESS + REALITY"
echo "  Порт:        $(grep '^REALITY_PORT=' "$ENV_FILE" | cut -d= -f2 || echo 2053)"
echo "  Public Key:  $PUBLIC_KEY"
echo "  Short ID:    $SHORT_ID"
echo ""
echo "  Для клиента (v2rayNG / Hiddify / NekoBox):"
echo "  ├─ Security:     reality"
echo "  ├─ SNI:          www.samsung.com"
echo "  ├─ Fingerprint:  chrome"
echo "  ├─ Public Key:   $PUBLIC_KEY"
echo "  ├─ Short ID:     $SHORT_ID"
echo "  └─ Flow:         xtls-rprx-vision"
echo ""
echo -e "${YELLOW}  ВАЖНО: Сохраните Public Key — он нужен клиентам!${NC}"
echo ""
