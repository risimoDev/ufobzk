#!/usr/bin/env bash
# ─────────────────────────────────────────────────
# 06-setup-warp.sh — Регистрация Cloudflare WARP
# и включение WireGuard outbound в Xray.
#
# Зачем: гео-база Google метит наш IPv4 как RU (поведенческий сигнал — через
# выход ходят почти только российские пользователи), хотя MaxMind/RIPE/Cloudflare
# видят NL. Лечится только тем, что трафик google/youtube выпускается другим
# путём. Этот скрипт заводит WARP-выход и прописывает ключи в .env; сам конфиг
# Xray собирает приложение (app/xray.py → build_xray_config).
#
# Запускать на ГЛАВНОМ сервере (docker-стек ufobzk-*).
#
#   bash scripts/06-setup-warp.sh             включить
#   bash scripts/06-setup-warp.sh --disable   выключить (быстрый откат)
#
# Перед включением стоит проверить, что WARP реально пропускает трафик:
#   bash scripts/15-diagnose-warp.sh
# ─────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
APP_CONTAINER="ufobzk-app"
XRAY_CONTAINER="ufobzk-xray"

[ -f "$ENV_FILE" ] || error ".env не найден: $ENV_FILE"
command -v python3 &>/dev/null || error "python3 не найден"

# ─── Откат ──────────────────────────────────────
if [ "${1:-}" = "--disable" ]; then
    info "Отключаем WARP (WARP_PRIVATE_KEY=)..."
    # Пустого PRIVATE_KEY достаточно: build_xray_config пропускает весь блок.
    # Ключи в .env не стираем — включить обратно можно тем же скриптом.
    if grep -q '^WARP_PRIVATE_KEY=' "$ENV_FILE"; then
        sed -i 's|^WARP_PRIVATE_KEY=.*|WARP_PRIVATE_KEY=|' "$ENV_FILE"
    fi
    cd "$PROJECT_DIR"
    # --build не нужен: код в образе уже актуален, меняется только окружение
    docker compose up -d --force-recreate --no-deps ufo-app

    for _ in $(seq 1 30); do
        if ! docker exec "$XRAY_CONTAINER" grep -q '"tag": "WARP"' /etc/xray/config.json 2>/dev/null; then
            info "WARP убран из конфига Xray — трафик снова идёт напрямую"
            exit 0
        fi
        sleep 2
    done
    error "WARP всё ещё в конфиге — смотрите: docker logs $APP_CONTAINER"
fi

# ─── Устанавливаем wgcf если нет ────────────────
# Версия на случай, если GitHub API недоступен или упёрлись в rate limit
WGCF_FALLBACK_VERSION="2.2.32"

if ! command -v wgcf &>/dev/null; then
    info "Устанавливаем wgcf..."
    ARCH=$(dpkg --print-architecture 2>/dev/null || echo "amd64")

    # Имя ассета содержит версию (wgcf_2.2.32_linux_amd64), поэтому путь
    # /releases/latest/download/wgcf_linux_amd64 отдаёт 404 — резолвим через API.
    WGCF_API=$(curl -fsSL --max-time 20 \
        https://api.github.com/repos/ViRb3/wgcf/releases/latest 2>/dev/null || true)

    WGCF_URL=""
    if [ -n "$WGCF_API" ]; then
        WGCF_URL=$(printf '%s' "$WGCF_API" | python3 -c '
import json, sys
arch = sys.argv[1]
data = json.load(sys.stdin)
want = "wgcf_%s_linux_%s" % (data["tag_name"].lstrip("v"), arch)
for asset in data.get("assets", []):
    if asset["name"] == want:
        print(asset["browser_download_url"])
        break
' "$ARCH" 2>/dev/null || true)
    fi

    if [ -z "$WGCF_URL" ]; then
        warn "GitHub API не ответил — ставим закреплённую версию $WGCF_FALLBACK_VERSION"
        WGCF_URL="https://github.com/ViRb3/wgcf/releases/download/v${WGCF_FALLBACK_VERSION}/wgcf_${WGCF_FALLBACK_VERSION}_linux_${ARCH}"
    fi

    info "Качаем: $WGCF_URL"
    curl -fsSL "$WGCF_URL" -o /usr/local/bin/wgcf \
        || error "Не удалось скачать wgcf: $WGCF_URL"
    chmod +x /usr/local/bin/wgcf

    /usr/local/bin/wgcf --help >/dev/null 2>&1 \
        || error "wgcf скачан, но не запускается (архитектура $ARCH?)"
    info "wgcf установлен"
fi

# ─── Регистрация WARP ───────────────────────────
WARP_DIR="$PROJECT_DIR/.warp"
mkdir -p "$WARP_DIR"
cd "$WARP_DIR"

if [ ! -f wgcf-account.toml ]; then
    info "Регистрация нового аккаунта WARP..."
    wgcf register --accept-tos
else
    info "Используем существующий аккаунт WARP (.warp/wgcf-account.toml)"
fi

info "Генерация конфигурации WireGuard..."
wgcf generate --profile wgcf-profile.conf >/dev/null
[ -f wgcf-profile.conf ] || error "Не удалось сгенерировать wgcf-profile.conf"

# ─── Извлекаем параметры ────────────────────────
# Разбираем питоном, а не awk/grep: значение отделяется ПЕРВЫМ '=' (в base64-ключах
# есть padding '='), а адреса wgcf может писать и отдельными строками, и одной
# строкой через запятую — v4/v6 различаем по наличию ':'.
mapfile -t WGCF_PARSED < <(python3 - wgcf-profile.conf <<'PYEOF'
import sys

values = {"PrivateKey": "", "PublicKey": "", "Endpoint": ""}
v4 = v6 = ""

with open(sys.argv[1], encoding="utf-8") as fh:
    for line in fh:
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key in values and not values[key]:
            values[key] = value
        elif key == "Address":
            for addr in value.split(","):
                addr = addr.strip().split("/")[0]
                if ":" in addr and not v6:
                    v6 = addr
                elif "." in addr and not v4:
                    v4 = addr

print(values["PrivateKey"])
print(values["PublicKey"])
print(values["Endpoint"])
print(v4)
print(v6)
PYEOF
)

WARP_PRIVATE="${WGCF_PARSED[0]:-}"
WARP_PUBLIC="${WGCF_PARSED[1]:-}"
WARP_ENDPOINT="${WGCF_PARSED[2]:-}"
WARP_ADDRESS4="${WGCF_PARSED[3]:-}"
WARP_ADDRESS6="${WGCF_PARSED[4]:-}"

[ -n "$WARP_PRIVATE"  ] || error "Не удалось извлечь PrivateKey из wgcf-profile.conf"
[ -n "$WARP_PUBLIC"   ] || error "Не удалось извлечь PublicKey из wgcf-profile.conf"
[ -n "$WARP_ADDRESS4" ] || error "Не удалось извлечь IPv4-адрес из wgcf-profile.conf"
[ -n "$WARP_ENDPOINT" ] || WARP_ENDPOINT="engage.cloudflareclient.com:2408"

# Фиксируем IPv4-литерал эндпоинта. Контейнер xray в docker-сети backend без
# IPv6: если engage.cloudflareclient.com отрезолвится в AAAA, отправка пакетов
# падает с "sendto: network is unreachable", и WARP молча не работает.
EP_HOST="${WARP_ENDPOINT%:*}"
EP_PORT="${WARP_ENDPOINT##*:}"
if ! printf '%s' "$EP_HOST" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
    EP_IP=$(python3 -c '
import socket, sys
print(socket.getaddrinfo(sys.argv[1], None, socket.AF_INET)[0][4][0])
' "$EP_HOST" 2>/dev/null || true)
    if [ -n "$EP_IP" ]; then
        info "Эндпоинт $EP_HOST → $EP_IP (фиксируем IPv4)"
        WARP_ENDPOINT="${EP_IP}:${EP_PORT}"
    else
        warn "Не удалось отрезолвить $EP_HOST в IPv4 — оставляем как есть"
    fi
fi

info "WARP PrivateKey: ${WARP_PRIVATE:0:8}..."
info "WARP Address v4: $WARP_ADDRESS4"
info "WARP Address v6: ${WARP_ADDRESS6:-<нет>}"
info "WARP Endpoint:   $WARP_ENDPOINT"

# ─── reserved: 3 байта из client_id аккаунта ────
# Cloudflare маршрутизирует сессию по этим байтам. Без них хендшейк обычно
# проходит, но соединение может молча не передавать трафик — поэтому тянем
# client_id из API и считаем честно, с откатом на [0,0,0].
WARP_RESERVED="0,0,0"
ACCESS_TOKEN=$(awk -F"'" '/^access_token/{print $2}' wgcf-account.toml 2>/dev/null || true)
DEVICE_ID=$(awk -F"'" '/^device_id/{print $2}' wgcf-account.toml 2>/dev/null || true)

if [ -n "$ACCESS_TOKEN" ] && [ -n "$DEVICE_ID" ]; then
    REG_JSON=$(curl -fsSL --max-time 15 \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -H "User-Agent: okhttp/3.12.1" \
        -H "CF-Client-Version: a-6.10-2158" \
        "https://api.cloudflareclient.com/v0a2158/reg/$DEVICE_ID" 2>/dev/null || true)

    if [ -n "$REG_JSON" ]; then
        COMPUTED=$(printf '%s' "$REG_JSON" | python3 -c '
import base64, json, sys
try:
    cid = json.load(sys.stdin).get("config", {}).get("client_id", "")
    raw = base64.b64decode(cid)
    if len(raw) == 3:
        print(",".join(str(b) for b in raw))
except Exception:
    pass
' 2>/dev/null || true)
        if [ -n "$COMPUTED" ]; then
            WARP_RESERVED="$COMPUTED"
            info "Reserved вычислен из client_id: [$WARP_RESERVED]"
        else
            warn "client_id не разобран — reserved остаётся [0,0,0]"
        fi
    else
        warn "API Cloudflare недоступен — reserved остаётся [0,0,0]"
    fi
else
    warn "access_token/device_id не найдены в wgcf-account.toml — reserved [0,0,0]"
fi

# ─── Обновляем .env ─────────────────────────────
set_env() {
    local key="$1" value="$2"
    if grep -q "^${key}=" "$ENV_FILE"; then
        # value может содержать / и + (base64) — используем | как разделитель
        sed -i "s|^${key}=.*|${key}=${value}|" "$ENV_FILE"
    else
        echo "${key}=${value}" >> "$ENV_FILE"
    fi
}

# Для настраиваемых вручную ключей: не затираем то, что подобрал админ
set_env_if_absent() {
    local key="$1" value="$2"
    grep -q "^${key}=" "$ENV_FILE" || echo "${key}=${value}" >> "$ENV_FILE"
}

set_env WARP_PRIVATE_KEY "$WARP_PRIVATE"
set_env WARP_PUBLIC_KEY  "$WARP_PUBLIC"
set_env WARP_ADDRESS_V4  "$WARP_ADDRESS4"
set_env WARP_ADDRESS_V6  "$WARP_ADDRESS6"
set_env WARP_ENDPOINT    "$WARP_ENDPOINT"
set_env WARP_RESERVED    "$WARP_RESERVED"

# Ручные ручки — пишем один раз, чтобы их было видно в .env, и больше не трогаем
set_env_if_absent WARP_MTU "1280"
set_env_if_absent WARP_IPV6 "0"

info "Ключи записаны в $ENV_FILE"

# ─── Пересобираем и пересоздаём приложение ──────
# --build обязателен: Dockerfile делает `COPY . .`, код приложения запечён в
# образ, и без пересборки контейнер поднимется со старым app/xray.py (без блока
# WARP). --force-recreate обязателен отдельно: env_file читается при СОЗДАНИИ
# контейнера, обычный restart новые переменные не подхватит.
cd "$PROJECT_DIR"
info "Пересобираем и пересоздаём $APP_CONTAINER..."
docker compose up -d --build --force-recreate --no-deps ufo-app

info "Ждём пересборку конфига Xray приложением..."
for _ in $(seq 1 30); do
    if docker exec "$XRAY_CONTAINER" grep -q '"tag": "WARP"' /etc/xray/config.json 2>/dev/null; then
        break
    fi
    sleep 2
done

if ! docker exec "$XRAY_CONTAINER" grep -q '"tag": "WARP"' /etc/xray/config.json 2>/dev/null; then
    warn "WARP outbound не появился в /etc/xray/config.json. Логи $APP_CONTAINER:"
    docker logs --tail 40 "$APP_CONTAINER" 2>&1 || true
    echo ""
    warn "Проверьте, что WARP_* есть в окружении контейнера:"
    docker exec "$APP_CONTAINER" printenv | grep '^WARP_' || true
    error "WARP не включился"
fi
info "WARP outbound есть в конфиге Xray"

sleep 5
if ! docker ps --filter "name=$XRAY_CONTAINER" --filter "status=running" -q | grep -q .; then
    error "Контейнер $XRAY_CONTAINER не запущен — смотрите: docker logs $XRAY_CONTAINER"
fi

if docker logs --tail 50 "$XRAY_CONTAINER" 2>&1 | grep -qi "wireguard.*\(fail\|error\|handshake\)"; then
    warn "В логах Xray есть ошибки WireGuard:"
    docker logs --tail 50 "$XRAY_CONTAINER" 2>&1 | grep -i wireguard || true
    warn "Если трафик не идёт — попробуйте WARP_MTU=1420 или WARP_ENDPOINT=162.159.192.1:2408"
fi

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${GREEN} WARP outbound включён${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
# Печатаем то, что реально попало в конфиг, а не то, что было дефолтом когда-то
echo "  Через WARP идут (WARP_DOMAINS в .env, дефолт в app/xray.py):"
docker exec "$XRAY_CONTAINER" sh -c \
    'sed -n "/\"outboundTag\": \"WARP\"/,/]/p" /etc/xray/config.json' 2>/dev/null \
    | grep -oE '"(geosite|domain|regexp):[^"]*"' | tr -d '"' | sed 's/^/  ├─ /' || true
echo ""
echo "  Правило стоит ВЫШЕ каскадных правил — YouTube больше не может"
echo "  утечь в RU-хаб через geoip:ru (GGC-кэши в российских AS)."
echo ""
echo "  Address:  $WARP_ADDRESS4 (туннель IPv4-only, WARP_IPV6=0)"
echo "  Endpoint: $WARP_ENDPOINT"
echo "  Reserved: [$WARP_RESERVED]"
echo ""
echo -e "${YELLOW}  ПРОВЕРЯТЬ НАДО С КЛИЕНТА, НЕ С СЕРВЕРА.${NC}"
echo "  Запуск ipregion или curl прямо здесь идёт мимо xray — маршрут WARP"
echo "  применяется только к трафику, вошедшему через инбаунд. С сервера вы"
echo "  всегда увидите старое RU, это ничего не значит."
echo ""
echo "  Подключитесь клиентом и выполните на нём:"
echo "    curl -s https://www.youtube.com/ | grep -o '\"INNERTUBE_CONTEXT_GL\":\"..\"'"
echo "  Ожидается не RU. В браузере дополнительно сбросьте cookies"
echo "  google.com/youtube.com — иначе русский интерфейс останется из-за"
echo "  старой сессии, а не из-за IP."
echo ""
echo -e "${YELLOW}  WARP+ (быстрее, если бесплатный тормозит на 4K):${NC}"
echo "  cd $WARP_DIR && wgcf update --license 'XXXX-XXXX-XXXX' && bash $0"
echo ""
