#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  17-diagnose-geo.sh — Какую страну Google видит у КЛИЕНТОВ VPN
# ═══════════════════════════════════════════════════════════════════════════
#  ТОЛЬКО ЧТЕНИЕ. Ничего не меняет.
#
#  Зачем: ipregion.sh / curl в шелле сервера идут МИМО Xray и всегда
#  показывают «голый» IPv4 главного (Google = RU). Это не то, что видят
#  клиенты. Скрипт проверяет весь путь гео-выхода:
#    1. IPv6 на хосте          (без него GEO-V6 молча уходит в IPv4)
#    2. IPv6 в docker-сети backend
#    3. Выбранный гео-выход в config.json Xray
#    4. Страну Google/YouTube ЧЕРЕЗ Xray (DIAG-SOCKS 127.0.0.1:10808)
#
#  Запуск:  cd /opt/ufobzk && bash scripts/17-diagnose-geo.sh
#  Полный прогон ipregion через Xray:
#    bash <(curl -fsSL https://raw.githubusercontent.com/Davoyan/ipregion/main/ipregion.sh) -p 127.0.0.1:10808
# ═══════════════════════════════════════════════════════════════════════════

set +e

PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
cd "$PROJECT_DIR" 2>/dev/null

SOCKS="127.0.0.1:10808"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

sec() { echo; echo "═══════════════════════════════════════════════════════════"; echo "### $* ###"; echo "═══════════════════════════════════════════════════════════"; }
kv()  { printf "  %-28s %s\n" "$1:" "$2"; }
getenv() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"; }

# Страна, которую Google проставляет на www.google.com (как в ipregion.sh)
google_country() {
  curl -s --max-time 15 -A "$UA" "$@" https://www.google.com 2>/dev/null \
    | sed -n 's/.*"[a-z]\{2\}_\([A-Z]\{2\}\)".*/\1/p' | head -1
}
youtube_country() {
  curl -s --max-time 15 -A "$UA" "$@" https://www.youtube.com 2>/dev/null \
    | grep -o '"countryCode":"[A-Z]*"' | head -1 | cut -d'"' -f4
}

# ── 1. IPv6 на хосте ──
sec "1. IPv6 НА ХОСТЕ"
V6_ADDR=$(ip -6 addr show scope global 2>/dev/null | awk '/inet6/{print $2}' | head -3)
V6_ROUTE=$(ip -6 route show default 2>/dev/null | head -1)
kv "глобальный IPv6" "${V6_ADDR:-НЕТ}"
kv "default route v6" "${V6_ROUTE:-НЕТ}"
V6_EXT=$(curl -6 -s --max-time 6 https://api64.ipify.org 2>/dev/null)
kv "внешний IPv6 (curl -6)" "${V6_EXT:-НЕ РАБОТАЕТ}"
if [ -z "$V6_EXT" ]; then
  echo "  ❌ У хоста нет рабочего IPv6. GEO-V6 невозможен: Xray уйдёт в IPv4 → Google = RU."
  echo "     Включите IPv6 в панели хостера и пропишите адрес/шлюз в netplan,"
  echo "     либо используйте GEO_NODE_NAME (выход через ноду)."
else
  kv "Google по IPv6 хоста" "$(google_country -6)"
fi
kv "Google по IPv4 хоста" "$(google_country -4)"

# ── 2. IPv6 в docker ──
sec "2. DOCKER-СЕТЬ BACKEND"
NET=$(docker network ls --format '{{.Name}}' | grep -E '_backend$' | head -1)
kv "сеть" "${NET:-не найдена}"
if [ -n "$NET" ]; then
  kv "EnableIPv6" "$(docker network inspect "$NET" -f '{{.EnableIPv6}}' 2>/dev/null)"
  kv "подсети" "$(docker network inspect "$NET" -f '{{range .IPAM.Config}}{{.Subnet}} {{end}}' 2>/dev/null)"
fi
kv "docker" "$(docker version -f '{{.Server.Version}}' 2>/dev/null)"
APP_V6=$(docker exec ufobzk-app python -c "
import socket
try:
    socket.create_connection(('2001:4860:4860::8888', 443), timeout=4).close(); print('OK')
except OSError as e:
    print('НЕТ:', e)
" 2>/dev/null)
kv "IPv6-выход из backend" "${APP_V6:-не удалось проверить}"

# ── 3. Что выбрано в конфиге ──
sec "3. ГЕО-ВЫХОД В .env И config.json"
kv "GEO_NODE_NAME" "$(getenv GEO_NODE_NAME)"
kv "GEO_TRANSIT_UUID" "$( [ -n "$(getenv GEO_TRANSIT_UUID)" ] && echo задан || echo ПУСТО)"
kv "GEO_IPV6" "$(getenv GEO_IPV6)"
if [ "$(getenv GEO_IPV6)" = "auto" ]; then
  kv "GEO-AUTO выбрал" "$(docker exec ufobzk-app cat data/geo_family 2>/dev/null || echo "v4 (по умолчанию)")"
  echo "  История переключений: docker logs ufobzk-app 2>&1 | grep GEO-AUTO"
fi
kv "GEO_DOMAINS (доп.)" "$(getenv GEO_DOMAINS)"
docker exec ufobzk-xray cat /etc/xray/config.json 2>/dev/null | python3 -c "
import json, sys
try:
    cfg = json.load(sys.stdin)
except Exception as e:
    print('  config.json не прочитан:', e); sys.exit()
tags = [o.get('tag') for o in cfg.get('outbounds', [])]
print('  outbounds:', ', '.join(tags))
for r in cfg.get('routing', {}).get('rules', []):
    d = r.get('domain') or []
    if 'geosite:google' in d or 'geosite:youtube' in d:
        print('  Google/YouTube →', r.get('outboundTag'), '(%d доменов)' % len(d))
        break
else:
    print('  ❌ правила для geosite:google нет — задеплойте новую версию приложения')
print('  DIAG-SOCKS inbound:', 'есть' if any(i.get('tag') == 'DIAG-SOCKS' for i in cfg.get('inbounds', [])) else 'НЕТ')
"

# ── 4. Через Xray (как у клиентов) ──
sec "4. ЧЕРЕЗ XRAY (так видят клиенты)"
if ! curl -s --max-time 5 --socks5-hostname "$SOCKS" -o /dev/null https://www.gstatic.com/generate_204; then
  echo "  ❌ $SOCKS недоступен. Пересоздайте xray: docker compose up -d xray"
else
  kv "внешний IP (не-Google)" "$(curl -s --max-time 8 --socks5-hostname "$SOCKS" https://api64.ipify.org)"
  kv "Google" "$(google_country --socks5-hostname "$SOCKS")"
  kv "YouTube" "$(youtube_country --socks5-hostname "$SOCKS")"
  echo
  echo "  Полная проверка:"
  echo "  bash <(curl -fsSL https://raw.githubusercontent.com/Davoyan/ipregion/main/ipregion.sh) -p $SOCKS"
fi
