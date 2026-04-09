#!/usr/bin/env bash
# ─────────────────────────────────────────────────
# 05-setup-reality.sh — Генерация ключей REALITY
# и автоматическое обновление конфигурации
# ─────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
XRAY_CONFIG="$PROJECT_DIR/xray_config.json"
CONTAINER_NAME="vpnbzk-marzban"

# ─── Проверки ────────────────────────────────────
[ -f "$ENV_FILE" ] || error ".env не найден: $ENV_FILE"
[ -f "$XRAY_CONFIG" ] || error "xray_config.json не найден: $XRAY_CONFIG"

# ─── Проверяем что контейнер Marzban запущен ─────
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    warn "Контейнер $CONTAINER_NAME не запущен. Попробуем запустить..."
    cd "$PROJECT_DIR"
    docker compose up -d marzban
    sleep 5
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

# ─── Записываем ключи в .env ────────────────────
# Обновляем REALITY_PRIVATE_KEY
if grep -q "^REALITY_PRIVATE_KEY=" "$ENV_FILE"; then
    sed -i "s|^REALITY_PRIVATE_KEY=.*|REALITY_PRIVATE_KEY=$PRIVATE_KEY|" "$ENV_FILE"
else
    echo "REALITY_PRIVATE_KEY=$PRIVATE_KEY" >> "$ENV_FILE"
fi

# Обновляем REALITY_PUBLIC_KEY
if grep -q "^REALITY_PUBLIC_KEY=" "$ENV_FILE"; then
    sed -i "s|^REALITY_PUBLIC_KEY=.*|REALITY_PUBLIC_KEY=$PUBLIC_KEY|" "$ENV_FILE"
else
    echo "REALITY_PUBLIC_KEY=$PUBLIC_KEY" >> "$ENV_FILE"
fi

info "Ключи записаны в .env"

# ─── Подставляем privateKey в xray_config.json ───
# Используем python (есть в контейнере) для безопасной JSON-правки
python3 - "$XRAY_CONFIG" "$PRIVATE_KEY" <<'PYEOF'
import json, sys

config_path, private_key = sys.argv[1], sys.argv[2]

with open(config_path, "r") as f:
    config = json.load(f)

# Находим REALITY inbound и ставим privateKey
for inbound in config.get("inbounds", []):
    ss = inbound.get("streamSettings", {})
    if "realitySettings" in ss:
        ss["realitySettings"]["privateKey"] = private_key

with open(config_path, "w") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print(f"[OK] privateKey записан в {config_path}")
PYEOF

# ─── Генерация Short ID ─────────────────────────
SHORT_ID=$(openssl rand -hex 8)
info "Short ID: $SHORT_ID"

python3 - "$XRAY_CONFIG" "$SHORT_ID" <<'PYEOF'
import json, sys

config_path, short_id = sys.argv[1], sys.argv[2]

with open(config_path, "r") as f:
    config = json.load(f)

for inbound in config.get("inbounds", []):
    ss = inbound.get("streamSettings", {})
    if "realitySettings" in ss:
        ss["realitySettings"]["shortIds"] = ["", short_id]

with open(config_path, "w") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
PYEOF

# ─── Перезапуск Marzban ─────────────────────────
info "Перезапуск Marzban для применения новой конфигурации..."
cd "$PROJECT_DIR"
docker compose restart marzban
sleep 3

info "Проверка что Marzban запущен..."
docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$" \
    || error "Marzban не запустился после перезапуска!"

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN} REALITY настроен успешно!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo "  Протокол:    VLESS + REALITY"
echo "  Порт:        $(grep '^REALITY_PORT=' "$ENV_FILE" | cut -d= -f2 || echo 2053)"
echo "  Dest:        $(grep '^REALITY_DEST=' "$ENV_FILE" | cut -d= -f2 || echo www.google.com:443)"
echo "  Public Key:  $PUBLIC_KEY"
echo "  Short ID:    $SHORT_ID"
echo ""
echo "  Для клиента (v2rayNG / Hiddify / NekoBox):"
echo "  ├─ Security:     reality"
echo "  ├─ SNI:          www.google.com"
echo "  ├─ Fingerprint:  chrome"
echo "  ├─ Public Key:   $PUBLIC_KEY"
echo "  ├─ Short ID:     $SHORT_ID"
echo "  └─ Flow:         xtls-rprx-vision"
echo ""
echo -e "${YELLOW}  ВАЖНО: Сохраните Public Key — он нужен клиентам!${NC}"
echo ""
