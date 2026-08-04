#!/usr/bin/env bash
# ─────────────────────────────────────────────────
# 16-warp-candidates.sh — Какие домены Google идут МИМО WARP
#
# Разбирает access.log Xray и показывает, какие google-домены ушли в DIRECT,
# а какие уже идут через WARP. Нужен, чтобы не угадывать бэкенды сервисов
# (Antigravity, Gemini и т.п.), а увидеть их в собственном трафике.
#
#   bash scripts/16-warp-candidates.sh              разбор накопленного лога
#   bash scripts/16-warp-candidates.sh --watch 120  живой сбор 120 секунд
#
# Из вывода берутся строки для WARP_DOMAINS в .env.
# Клиентские IP и email из лога не показываются — только домены и счётчики.
# ─────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

XRAY_CONTAINER="${XRAY_CONTAINER:-ufobzk-xray}"
ACCESS_LOG="/var/log/xray/access.log"

command -v docker  &>/dev/null || error "docker не найден"
command -v python3 &>/dev/null || error "python3 не найден"

docker ps --filter "name=$XRAY_CONTAINER" --filter "status=running" -q | grep -q . \
    || error "Контейнер $XRAY_CONTAINER не запущен"

WATCH_SECONDS=0
if [ "${1:-}" = "--watch" ]; then
    WATCH_SECONDS="${2:-120}"
    printf '%s' "$WATCH_SECONDS" | grep -qE '^[0-9]+$' || error "--watch ожидает число секунд"
fi

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

if [ "$WATCH_SECONDS" -gt 0 ]; then
    info "Собираю трафик $WATCH_SECONDS секунд — откройте нужный сервис на клиенте..."
    # timeout завершает tail по истечении срока; код 124 при этом штатный
    docker exec "$XRAY_CONTAINER" sh -c \
        "timeout ${WATCH_SECONDS} tail -n 0 -f ${ACCESS_LOG}" \
        > "$TMP_DIR/access.log" 2>/dev/null || true
    info "Сбор завершён"
else
    info "Разбираю накопленный $ACCESS_LOG (для живого сбора: --watch 120)"
    docker exec "$XRAY_CONTAINER" cat "$ACCESS_LOG" > "$TMP_DIR/access.log" 2>/dev/null \
        || error "Не удалось прочитать $ACCESS_LOG"
fi

[ -s "$TMP_DIR/access.log" ] || error "Лог пуст — трафика за это время не было"

python3 - "$TMP_DIR/access.log" <<'PYEOF'
import re
import sys
from collections import Counter

# accepted tcp:gemini.google.com:443 [VLESS-WS -> DIRECT] email: ...
LINE = re.compile(r"accepted\s+\w+:([^\s:]+):\d+\s+\[[^\]]*->\s*([A-Za-z0-9_-]+)\]")

# Что считаем «относящимся к Google». Сознательно широко: задача — показать
# кандидатов, решение о добавлении принимает человек.
GOOGLE_HINTS = (
    "google", "gstatic", "googleapis", "googleusercontent", "googlevideo",
    "ggpht", "ytimg", "youtube", "youtu.be", "withgoogle", "antigravity",
    "gvt1.com", "gvt2.com",
)

direct = Counter()
via_warp = Counter()
other_tags = Counter()
direct_rest = Counter()

with open(sys.argv[1], encoding="utf-8", errors="replace") as fh:
    for line in fh:
        match = LINE.search(line)
        if not match:
            continue
        domain, tag = match.group(1), match.group(2)
        low = domain.lower()
        if not any(hint in low for hint in GOOGLE_HINTS):
            # Бэкенд сервиса может жить где угодно — Antigravity, например,
            # построен на технологии Windsurf/Codeium. Поэтому всё остальное,
            # что ушло мимо WARP, тоже показываем, а не молча отбрасываем.
            if tag == "DIRECT":
                direct_rest[domain] += 1
            continue
        if tag == "WARP":
            via_warp[domain] += 1
        elif tag == "DIRECT":
            direct[domain] += 1
        else:
            other_tags[(domain, tag)] += 1

def show(title, counter, limit=40):
    print()
    print(title)
    if not counter:
        print("    (пусто)")
        return
    for name, count in counter.most_common(limit):
        if isinstance(name, tuple):
            print(f"    {count:>6}  {name[0]}  -> {name[1]}")
        else:
            print(f"    {count:>6}  {name}")

show("═══ Google-домены МИМО WARP (-> DIRECT) — кандидаты ═══", direct)
show("═══ Уже идут через WARP ═══", via_warp)
if other_tags:
    show("═══ Ушли в другой outbound (проверьте, так ли надо) ═══", other_tags)
show("═══ Остальные домены мимо WARP (топ-30) ═══", direct_rest, limit=30)
print("    Бэкенд нужного сервиса может быть здесь, а не среди google-доменов.")

if direct:
    print()
    print("Строки для WARP_DOMAINS (добавляйте только нужные, через запятую):")
    print()
    for name, _ in direct.most_common(40):
        print(f"    domain:{name}")
    print()
    print("ВНИМАНИЕ: не заворачивайте в WARP всё подряд. connectivitycheck.gstatic.com,")
    print("dns.google, mtalk.google.com и общий googleapis.com нужны Android для")
    print("проверки связности и пушей — если WARP просядет, телефоны сочтут, что")
    print("интернета нет, при живом туннеле.")
PYEOF
