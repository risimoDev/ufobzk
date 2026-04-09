#!/usr/bin/env bash
# ─────────────────────────────────────────────────
# 06-setup-warp.sh — Регистрация Cloudflare WARP
# и настройка WireGuard outbound в Xray
# ─────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
XRAY_CONFIG="$PROJECT_DIR/xray_config.json"

[ -f "$ENV_FILE" ] || error ".env не найден: $ENV_FILE"
[ -f "$XRAY_CONFIG" ] || error "xray_config.json не найден: $XRAY_CONFIG"

# ─── Устанавливаем wgcf если нет ────────────────
if ! command -v wgcf &>/dev/null; then
    info "Устанавливаем wgcf..."
    ARCH=$(dpkg --print-architecture 2>/dev/null || echo "amd64")
    WGCF_URL="https://github.com/ViRb3/wgcf/releases/latest/download/wgcf_linux_${ARCH}"
    curl -fsSL "$WGCF_URL" -o /usr/local/bin/wgcf
    chmod +x /usr/local/bin/wgcf
    info "wgcf установлен"
fi

# ─── Регистрация WARP ───────────────────────────
WARP_DIR="$PROJECT_DIR/.warp"
mkdir -p "$WARP_DIR"
cd "$WARP_DIR"

if [ ! -f wgcf-account.toml ]; then
    info "Регистрация нового аккаунта WARP..."
    wgcf register --accept-tos
fi

info "Генерация конфигурации WireGuard..."
wgcf generate
[ -f wgcf-profile.conf ] || error "Не удалось сгенерировать wgcf-profile.conf"

# ─── Извлекаем параметры ────────────────────────
WARP_PRIVATE=$(grep "^PrivateKey" wgcf-profile.conf | awk '{print $3}')
WARP_ADDRESS4=$(grep "^Address" wgcf-profile.conf | head -1 | awk '{print $3}' | cut -d/ -f1)
WARP_ADDRESS6=$(grep "^Address" wgcf-profile.conf | tail -1 | awk '{print $3}' | cut -d/ -f1)
WARP_PUBLIC=$(grep "^PublicKey" wgcf-profile.conf | awk '{print $3}')
WARP_ENDPOINT=$(grep "^Endpoint" wgcf-profile.conf | awk '{print $3}')

[ -n "$WARP_PRIVATE" ] || error "Не удалось извлечь PrivateKey из wgcf-profile.conf"

info "WARP PrivateKey: ${WARP_PRIVATE:0:8}..."
info "WARP Address v4: $WARP_ADDRESS4"
info "WARP Address v6: $WARP_ADDRESS6"

# ─── Генерация reserved bytes ───────────────────
# wgcf не даёт reserved напрямую; используем API Cloudflare
# Для простоты ставим [0,0,0] — работает в большинстве случаев
WARP_RESERVED="0,0,0"
info "Reserved: [$WARP_RESERVED] (по умолчанию)"

# ─── Обновляем .env ─────────────────────────────
if grep -q "^WARP_PRIVATE_KEY=" "$ENV_FILE"; then
    sed -i "s|^WARP_PRIVATE_KEY=.*|WARP_PRIVATE_KEY=$WARP_PRIVATE|" "$ENV_FILE"
else
    echo "WARP_PRIVATE_KEY=$WARP_PRIVATE" >> "$ENV_FILE"
fi

if grep -q "^WARP_RESERVED=" "$ENV_FILE"; then
    sed -i "s|^WARP_RESERVED=.*|WARP_RESERVED=$WARP_RESERVED|" "$ENV_FILE"
else
    echo "WARP_RESERVED=$WARP_RESERVED" >> "$ENV_FILE"
fi

info "Ключи записаны в .env"

# ─── Обновляем xray_config.json ─────────────────
python3 - "$XRAY_CONFIG" "$WARP_PRIVATE" "$WARP_PUBLIC" "$WARP_ADDRESS4" "$WARP_ADDRESS6" "$WARP_ENDPOINT" <<'PYEOF'
import json, sys

config_path = sys.argv[1]
private_key = sys.argv[2]
public_key  = sys.argv[3]
addr4       = sys.argv[4]
addr6       = sys.argv[5]
endpoint    = sys.argv[6]

with open(config_path, "r") as f:
    config = json.load(f)

# Ищем существующий WARP outbound (любой регистр тега)
found = False
for outbound in config.get("outbounds", []):
    if outbound.get("tag", "").upper() == "WARP":
        outbound["settings"]["secretKey"] = private_key
        outbound["settings"]["address"] = [f"{addr4}/32", f"{addr6}/128"]
        peers = outbound.get("settings", {}).get("peers", [{}])
        if peers:
            peers[0]["publicKey"] = public_key
            peers[0]["endpoint"] = endpoint
        found = True

# Если нет — добавляем WARP outbound и routing правило
if not found:
    warp_outbound = {
        "tag": "WARP",
        "protocol": "wireguard",
        "settings": {
            "secretKey": private_key,
            "address": [f"{addr4}/32", f"{addr6}/128"],
            "peers": [{
                "publicKey": public_key,
                "allowedIPs": ["0.0.0.0/0", "::/0"],
                "endpoint": endpoint
            }],
            "reserved": [0, 0, 0],
            "mtu": 1280
        }
    }
    config["outbounds"].append(warp_outbound)

    # Добавляем routing правило для стриминговых/AI доменов (перед DIRECT)
    warp_rule = {
        "type": "field",
        "outboundTag": "WARP",
        "domain": [
            "geosite:openai",
            "geosite:netflix",
            "geosite:spotify",
            "geosite:disney"
        ]
    }
    rules = config.get("routing", {}).get("rules", [])
    # Вставляем перед последним правилом (DIRECT)
    if rules:
        rules.insert(-1, warp_rule)
    else:
        rules.append(warp_rule)

with open(config_path, "w") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print(f"[OK] WARP WireGuard настроен в {config_path}")
PYEOF

# ─── Перезапуск Marzban ─────────────────────────
info "Перезапуск Marzban..."
cd "$PROJECT_DIR"
docker compose restart marzban
sleep 3

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN} WARP outbound настроен!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo "  Трафик к следующим сервисам пойдёт через WARP:"
echo "  ├─ OpenAI (ChatGPT)"
echo "  ├─ Netflix"
echo "  ├─ Spotify"
echo "  ├─ Disney+"
echo "  └─ (можно добавить в xray_config.json → routing → rules)"
echo ""
echo "  WARP Address: $WARP_ADDRESS4 / $WARP_ADDRESS6"
echo "  Endpoint:     $WARP_ENDPOINT"
echo ""
echo -e "${YELLOW}  Если нужен WARP+ (быстрее), введите лицензию:${NC}"
echo "  wgcf update --name 'vpnbzk' --license 'XXXX-XXXX-XXXX'"
echo "  Затем перезапустите этот скрипт."
echo ""
