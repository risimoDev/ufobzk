#!/usr/bin/env bash
# ─────────────────────────────────────────────────
# 15-diagnose-warp.sh — Проверка, что WARP-выход реально пропускает трафик
#
# Поднимает ОТДЕЛЬНЫЙ временный контейнер xray с socks-инбаундом и WARP-аутбаундом
# и ходит через него наружу. Прод (ufobzk-xray) не трогается.
#
# Зачем: если WARP не передаёт трафик, а на него заведены домены Google, то на
# Android ломается connectivity check и телефон считает, что интернета нет —
# при живом туннеле. Поэтому WARP надо проверять ДО включения.
#
#   bash scripts/15-diagnose-warp.sh
# ─────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"
XRAY_CONTAINER="ufobzk-xray"
TEST_CONTAINER="ufobzk-warp-test"
TEST_PORT=10809
XRAY_IMAGE="teddysun/xray:latest"

[ -f "$ENV_FILE" ] || error ".env не найден: $ENV_FILE"
command -v python3 &>/dev/null || error "python3 не найден"
command -v docker  &>/dev/null || error "docker не найден"
curl --help all 2>/dev/null | grep -q -- --socks5-hostname \
    || error "curl без поддержки socks5 — поставьте нормальный curl"

env_get() {
    grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true
}

WARP_PRIVATE_KEY=$(env_get WARP_PRIVATE_KEY)
WARP_PUBLIC_KEY=$(env_get WARP_PUBLIC_KEY)
WARP_ADDRESS_V4=$(env_get WARP_ADDRESS_V4)
WARP_ADDRESS_V6=$(env_get WARP_ADDRESS_V6)
WARP_ENDPOINT=$(env_get WARP_ENDPOINT)
WARP_RESERVED=$(env_get WARP_RESERVED)

[ -n "$WARP_PRIVATE_KEY" ] || warn "WARP_PRIVATE_KEY пуст в .env (WARP сейчас выключен) — тестируем ключи из .warp"

# Если .env вычищен откатом — берём ключи прямо из профиля wgcf
if [ -z "$WARP_PRIVATE_KEY" ] && [ -f "$PROJECT_DIR/.warp/wgcf-profile.conf" ]; then
    mapfile -t P < <(python3 - "$PROJECT_DIR/.warp/wgcf-profile.conf" <<'PYEOF'
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
print(values["PrivateKey"]); print(values["PublicKey"])
print(values["Endpoint"]); print(v4); print(v6)
PYEOF
)
    WARP_PRIVATE_KEY="${P[0]:-}"
    WARP_PUBLIC_KEY="${P[1]:-}"
    WARP_ENDPOINT="${P[2]:-}"
    WARP_ADDRESS_V4="${P[3]:-}"
    WARP_ADDRESS_V6="${P[4]:-}"
fi

[ -n "$WARP_PRIVATE_KEY" ] || error "Нет ключей WARP: ни в .env, ни в .warp/wgcf-profile.conf"
[ -n "$WARP_ENDPOINT" ] || WARP_ENDPOINT="engage.cloudflareclient.com:2408"
[ -n "$WARP_RESERVED" ] || WARP_RESERVED="0,0,0"

cleanup() { docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

TMP_DIR=$(mktemp -d)
trap 'cleanup; rm -rf "$TMP_DIR"' EXIT

# ─── Страна глазами самого YouTube ──────────────
# Главная метрика: гео-базы (ipinfo, MaxMind) видят NL, а Google метит наш IPv4
# как RU — расходятся именно они. Домашняя страница YouTube отдаёт свою метку
# в INNERTUBE_CONTEXT_GL, это и есть мнение Google. Аргументы функции целиком
# передаются в curl (например --socks5-hostname 127.0.0.1:10809).
youtube_country() {
    local gl attempt
    # YouTube иногда режет частые запросы к полной странице — разовый пустой
    # ответ не должен читаться как «страна не определилась»
    for attempt in 1 2; do
        gl=$(curl -fsS --max-time 25 -H 'Accept-Language: en-US,en;q=0.9' "$@" \
            https://www.youtube.com/ 2>/dev/null \
            | grep -o '"INNERTUBE_CONTEXT_GL":"[A-Z][A-Z]"' | head -1 | cut -d'"' -f4 || true)
        if [ -n "$gl" ]; then
            printf '%s' "$gl"
            return 0
        fi
        [ "$attempt" -lt 2 ] && sleep 3
    done
    return 0
}

# ─── Базовая линия: прямой выход сервера ────────
echo ""
echo "═══ Базовая линия (прямой выход сервера) ═══"
DIRECT_JSON=$(curl -fsS --max-time 20 https://ipinfo.io/json 2>/dev/null || true)
if [ -n "$DIRECT_JSON" ]; then
    DIRECT_IP=$(printf '%s' "$DIRECT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ip",""))' 2>/dev/null || true)
    DIRECT_CC=$(printf '%s' "$DIRECT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("country",""))' 2>/dev/null || true)
    ok "Прямой выход: $DIRECT_IP ($DIRECT_CC по данным ipinfo)"
else
    warn "Не удалось получить прямой IP (ipinfo.io недоступен?)"
fi

DIRECT_GL=$(youtube_country)
if [ -n "$DIRECT_GL" ]; then
    if [ "$DIRECT_GL" = "RU" ]; then
        fail "Прямой выход: YouTube считает страну RU — это и есть проблема"
    else
        warn "Прямой выход: YouTube считает страну $DIRECT_GL (не RU — возможно, чинить уже нечего)"
    fi
else
    warn "Не удалось определить страну YouTube для прямого выхода"
fi

# ─── Тест одной конфигурации WARP ───────────────
# $1 — человекочитаемое имя, $2 — reserved, $3 — mtu, $4 — endpoint
test_warp() {
    local label="$1" reserved="$2" mtu="$3" endpoint="$4"

    python3 - "$TMP_DIR/config.json" "$WARP_PRIVATE_KEY" "$WARP_PUBLIC_KEY" \
        "$WARP_ADDRESS_V4" "$WARP_ADDRESS_V6" "$endpoint" "$reserved" "$mtu" <<'PYEOF'
import json, sys

path, priv, pub, v4, v6, endpoint, reserved, mtu = sys.argv[1:9]

address = [f"{v4}/32"]
if ":" in v6:
    address.append(f"{v6}/128")

try:
    reserved_bytes = [int(x) for x in reserved.split(",")]
    if len(reserved_bytes) != 3:
        reserved_bytes = [0, 0, 0]
except ValueError:
    reserved_bytes = [0, 0, 0]

config = {
    "log": {"loglevel": "debug"},
    "inbounds": [{
        "tag": "socks-in",
        "listen": "0.0.0.0",
        "port": 10808,
        "protocol": "socks",
        "settings": {"auth": "noauth", "udp": True}
    }],
    "outbounds": [{
        "tag": "WARP",
        "protocol": "wireguard",
        "settings": {
            "secretKey": priv,
            "address": address,
            "peers": [{
                "publicKey": pub,
                "allowedIPs": ["0.0.0.0/0", "::/0"],
                "endpoint": endpoint
            }],
            "reserved": reserved_bytes,
            "mtu": int(mtu)
        }
    }]
}

with open(path, "w", encoding="utf-8") as fh:
    json.dump(config, fh, indent=2)
PYEOF

    docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
    docker run -d --rm --name "$TEST_CONTAINER" \
        -p "127.0.0.1:${TEST_PORT}:10808" \
        -v "$TMP_DIR/config.json:/etc/xray/config.json:ro" \
        --entrypoint xray \
        "$XRAY_IMAGE" run -config /etc/xray/config.json >/dev/null 2>&1 \
        || { fail "$label — не удалось запустить тестовый контейнер"; return 1; }

    sleep 5

    if ! docker ps --filter "name=$TEST_CONTAINER" --filter "status=running" -q | grep -q .; then
        fail "$label — тестовый xray упал:"
        docker logs "$TEST_CONTAINER" 2>&1 | tail -15 || true
        return 1
    fi

    # Xray поднимает сессию WireGuard лениво — на первом же пакете. Первый запрос
    # часто уходит в никуда, пока идёт handshake, поэтому греем туннель и
    # повторяем: одна попытка сразу после старта ничего не доказывает.
    curl -fsS --max-time 15 --socks5-hostname "127.0.0.1:${TEST_PORT}" \
        https://cloudflare.com/cdn-cgi/trace -o /dev/null 2>/dev/null || true

    local json="" ip cc attempt
    for attempt in 1 2 3; do
        json=$(curl -fsS --max-time 25 --socks5-hostname "127.0.0.1:${TEST_PORT}" \
            https://ipinfo.io/json 2>/dev/null || true)
        [ -n "$json" ] && break
        [ "$attempt" -lt 3 ] && sleep 4
    done

    if [ -z "$json" ]; then
        fail "$label — трафик через WARP НЕ идёт (3 попытки)"
        echo "      Логи тестового xray:"
        docker logs "$TEST_CONTAINER" 2>&1 | grep -iE "wireguard|handshake|fail|error" | tail -10 \
            || docker logs "$TEST_CONTAINER" 2>&1 | tail -10 || true
        docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
        return 1
    fi

    ip=$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("ip",""))' 2>/dev/null || true)
    cc=$(printf '%s' "$json" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("country",""))' 2>/dev/null || true)
    ok "$label — работает: $ip ($cc)"

    local gcc
    gcc=$(curl -fsS --max-time 25 --socks5-hostname "127.0.0.1:${TEST_PORT}" \
        "https://www.google.com/generate_204" -o /dev/null -w '%{http_code}' 2>/dev/null || true)
    if [ "$gcc" = "204" ]; then
        ok "$label — google.com отвечает через WARP"
    else
        warn "$label — google.com через WARP вернул '$gcc' (ожидался 204)"
    fi

    # Главное: что о стране думает САМ YouTube, а не гео-базы вроде ipinfo.
    # Именно эта метка ломала выдачу, и только она подтверждает, что стало лучше.
    local gl
    gl=$(youtube_country --socks5-hostname "127.0.0.1:${TEST_PORT}")
    if [ -z "$gl" ]; then
        warn "$label — не удалось определить страну YouTube"
    elif [ "$gl" = "RU" ]; then
        fail "$label — YouTube всё равно считает выход российским (GL=RU)"
    else
        ok "$label — YouTube определяет страну как $gl"
    fi

    WORKING_RESERVED="$reserved"
    WORKING_MTU="$mtu"
    WORKING_ENDPOINT="$endpoint"
    docker rm -f "$TEST_CONTAINER" >/dev/null 2>&1 || true
    return 0
}

echo ""
echo "═══ Проверка WARP-выхода ═══"
info "Endpoint: $WARP_ENDPOINT | reserved: [$WARP_RESERVED] | address: $WARP_ADDRESS_V4"

WORKING_RESERVED=""
WORKING_MTU=""
WORKING_ENDPOINT=""
SUCCESS=0

if test_warp "текущая конфигурация" "$WARP_RESERVED" "1280" "$WARP_ENDPOINT"; then
    SUCCESS=1
else
    echo ""
    warn "Основная конфигурация не работает — перебираем варианты"

    for variant in \
        "reserved [0,0,0]|0,0,0|1280|$WARP_ENDPOINT" \
        "MTU 1420|$WARP_RESERVED|1420|$WARP_ENDPOINT" \
        "endpoint 162.159.192.1:2408|$WARP_RESERVED|1280|162.159.192.1:2408" \
        "endpoint 162.159.193.10:2408|$WARP_RESERVED|1280|162.159.193.10:2408"
    do
        IFS='|' read -r vlabel vres vmtu vep <<<"$variant"
        if test_warp "$vlabel" "$vres" "$vmtu" "$vep"; then
            SUCCESS=1
            break
        fi
    done
fi

# ─── Что говорит прод ───────────────────────────
echo ""
echo "═══ Логи прод-xray ($XRAY_CONTAINER) ═══"
if docker ps --filter "name=$XRAY_CONTAINER" --filter "status=running" -q | grep -q .; then
    WG_ERRORS=$(docker exec "$XRAY_CONTAINER" sh -c \
        'tail -300 /var/log/xray/error.log 2>/dev/null' 2>/dev/null \
        | grep -iE "wireguard|handshake" | tail -10 || true)
    if [ -n "$WG_ERRORS" ]; then
        warn "Записи про wireguard в error.log:"
        printf '%s\n' "$WG_ERRORS"
    else
        info "Записей про wireguard в error.log нет"
    fi

    if docker exec "$XRAY_CONTAINER" grep -q '"tag": "WARP"' /etc/xray/config.json 2>/dev/null; then
        info "WARP сейчас ВКЛЮЧЁН в проде"
        echo "  Домены на WARP:"
        docker exec "$XRAY_CONTAINER" sh -c \
            'sed -n "/\"outboundTag\": \"WARP\"/,/]/p" /etc/xray/config.json' 2>/dev/null \
            | grep -oE '"(geosite|domain|regexp):[^"]*"' | sed 's/^/    /' || true
    else
        info "WARP сейчас ВЫКЛЮЧЕН в проде"
    fi
else
    warn "Контейнер $XRAY_CONTAINER не запущен"
fi

# ─── Вывод ──────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════"
if [ "$SUCCESS" = "1" ]; then
    echo -e "${GREEN} WARP пропускает трафик${NC}"
    echo "═══════════════════════════════════════════════════"
    echo ""
    echo "  Рабочие параметры:"
    echo "    WARP_RESERVED=$WORKING_RESERVED"
    echo "    WARP_MTU=$WORKING_MTU"
    echo "    WARP_ENDPOINT=$WORKING_ENDPOINT"
    echo ""
    echo "  Если они отличаются от текущих — пропишите в .env и включите:"
    echo "    bash scripts/06-setup-warp.sh"
    echo ""
    echo "  Прогоните скрипт ещё раз перед включением: WireGuard поднимает"
    echo "  сессию лениво, и разница между вариантами может оказаться просто"
    echo "  прогревом туннеля, а не параметром. Если с первого варианта всё"
    echo "  зелёное — параметры менять не нужно."
else
    echo -e "${RED} WARP НЕ пропускает трафик — включать нельзя${NC}"
    echo "═══════════════════════════════════════════════════"
    echo ""
    echo "  Ни один вариант не заработал. Вероятные причины:"
    echo "  ├─ UDP 2408 наружу режется хостером/файрволом"
    echo "  ├─ аккаунт WARP забанен для этого IP (Cloudflare это делает)"
    echo "  └─ версия Xray в образе без поддержки wireguard-аутбаунда"
    echo ""
    echo "  Проверка UDP наружу:"
    echo "    nc -zvu 162.159.192.1 2408"
    echo ""
    echo "  Оставьте WARP выключенным:"
    echo "    bash scripts/06-setup-warp.sh --disable"
fi
echo ""
