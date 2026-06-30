#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  13-diagnose-new-server.sh — Диагностика главного сервера VPNBZK
# ═══════════════════════════════════════════════════════════════════════════
#  ТОЛЬКО ЧТЕНИЕ. Ничего не меняет. Секреты маскируются.
#  Цель: собрать полный отчёт о состоянии стека, чтобы понять что чинить.
#
#  Запуск (из каталога проекта, от root):
#     cd /opt/ufobzk && bash scripts/13-diagnose-new-server.sh
#  Отправьте ВЕСЬ вывод (можно в файл: ... | tee /tmp/diag.txt)
# ═══════════════════════════════════════════════════════════════════════════

set +e  # диагностика не должна падать на ошибках

PROJECT_DIR="${PROJECT_DIR:-$(pwd)}"
cd "$PROJECT_DIR" 2>/dev/null

DC="docker compose"
$DC version >/dev/null 2>&1 || DC="docker-compose"

sec() { echo; echo "═══════════════════════════════════════════════════════════"; echo "### $* ###"; echo "═══════════════════════════════════════════════════════════"; }
kv()  { printf "  %-22s %s\n" "$1:" "$2"; }

# Маскировка секретов: показываем длину + хвостик
mask() {
  local v="$1"; local n=${#v}
  if [ -z "$v" ]; then echo "(ПУСТО)"; elif [ "$n" -le 8 ]; then echo "*** (len=$n)"; else echo "${v:0:4}…${v: -4} (len=$n)"; fi
}
# Прочитать переменную из .env без выполнения файла
getenv() { grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"; }

echo "VPNBZK DIAGNOSTIC REPORT"
echo "generated: $(date -u '+%Y-%m-%d %H:%M:%S') UTC"

# ── 0. Система ──
sec "0. СИСТЕМА"
kv "hostname" "$(hostname)"
kv "external IP" "$(curl -4 -s --max-time 5 ifconfig.me 2>/dev/null || echo '?')"
kv "project dir" "$PROJECT_DIR"
kv "docker" "$(docker --version 2>/dev/null)"
kv "compose" "$($DC version --short 2>/dev/null)"
kv "uptime" "$(uptime -p 2>/dev/null)"
echo "  git:"; git -C "$PROJECT_DIR" log --oneline -1 2>/dev/null | sed 's/^/    /'
git -C "$PROJECT_DIR" status --short 2>/dev/null | head -10 | sed 's/^/    /'

# ── 1. .env (секреты замаскированы) ──
sec "1. .env"
if [ -f .env ]; then
  DOMAIN=$(getenv DOMAIN)
  kv "DOMAIN" "$DOMAIN"
  kv "WEBAPP_URL" "$(getenv WEBAPP_URL)"
  kv "NL_SERVER_IP" "$(getenv NL_SERVER_IP)"
  kv "RU_SERVER_IP" "$(getenv RU_SERVER_IP)"
  kv "REALITY_PORT" "$(getenv REALITY_PORT)"
  kv "REALITY_PUBLIC_KEY" "$(getenv REALITY_PUBLIC_KEY)"          # публичный — не секрет
  kv "REALITY_PRIVATE_KEY" "$(mask "$(getenv REALITY_PRIVATE_KEY)")"
  kv "REALITY_SHORT_ID" "$(getenv REALITY_SHORT_ID)"
  kv "REALITY_SERVER_NAMES" "$(getenv REALITY_SERVER_NAMES)"
  kv "XHTTP_MODE" "$(getenv XHTTP_MODE)"
  kv "RU_TRANSIT_UUID" "$(mask "$(getenv RU_TRANSIT_UUID)")"
  kv "RU_TRANSIT_PORT" "$(getenv RU_TRANSIT_PORT)"
  kv "RU_TRANSIT_PUBLIC_KEY" "$(getenv RU_TRANSIT_PUBLIC_KEY)"
  kv "RU_TRANSIT_SHORT_ID" "$(getenv RU_TRANSIT_SHORT_ID)"
  kv "RU_TRANSIT_SN" "$(getenv RU_TRANSIT_SN)"
  kv "SECRET_KEY" "$(mask "$(getenv SECRET_KEY)")"
  kv "ADMIN_PASSWORD" "$(mask "$(getenv ADMIN_PASSWORD)")"
  kv "XRAY_NODE_TOKEN" "$(mask "$(getenv XRAY_NODE_TOKEN)")"
  kv "METRICS_TOKEN" "$(mask "$(getenv METRICS_TOKEN)")"
  kv "SUPERADMIN_TELEGRAM_ID" "$(getenv SUPERADMIN_TELEGRAM_ID)"
  kv "TELEGRAM_BOT_TOKEN" "$(mask "$(getenv TELEGRAM_BOT_TOKEN)")"
else
  echo "  !!! .env НЕ НАЙДЕН в $PROJECT_DIR"
  DOMAIN=""
fi

# ── 2. Контейнеры ──
sec "2. КОНТЕЙНЕРЫ (docker compose ps -a)"
$DC ps -a 2>&1
echo; echo "  ожидаемые сервисы:"; $DC config --services 2>/dev/null | sed 's/^/    /'

# ── 3. Volumes ──
sec "3. VOLUMES"
docker volume ls --format '{{.Name}}' 2>/dev/null | grep -iE 'ufobzk|app-data|certbot|xray' | sed 's/^/  /'
APPVOL=$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep 'app-data' | head -1)
CRTVOL=$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep 'certbot-certs' | head -1)
XCFGVOL=$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep 'xray-config' | head -1)
APP_MP=$(docker volume inspect "$APPVOL" --format '{{.Mountpoint}}' 2>/dev/null)
CRT_MP=$(docker volume inspect "$CRTVOL" --format '{{.Mountpoint}}' 2>/dev/null)
XCFG_MP=$(docker volume inspect "$XCFGVOL" --format '{{.Mountpoint}}' 2>/dev/null)
kv "app-data vol" "$APPVOL"
kv "certbot vol" "$CRTVOL"
kv "xray-config vol" "$XCFGVOL"

# ── 4. База данных ──
sec "4. БАЗА ДАННЫХ"
if [ -n "$APP_MP" ] && [ -f "$APP_MP/vpnbzk.db" ]; then
  ls -lh "$APP_MP/vpnbzk.db" "$APP_MP"/vpnbzk.db-wal 2>/dev/null | sed 's/^/  /'
  python3 - "$APP_MP/vpnbzk.db" <<'PY' 2>&1 | sed 's/^/  /'
import sqlite3, sys
try:
    c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
    def q(s):
        try: return c.execute(s).fetchone()[0]
        except Exception as e: return "ERR(%s)" % e
    print("users        :", q("select count(*) from users"))
    print("vpn_keys     :", q("select count(*) from vpn_keys"))
    print("active_keys  :", q("select count(*) from vpn_keys where is_active=1"))
    print("servers      :", q("select count(*) from servers"))
    print("payments     :", q("select count(*) from payments"))
    print("snapshots    :", q("select count(*) from traffic_snapshots"))
    print("last_snapshot:", q("select max(recorded_at) from traffic_snapshots"))
    print("sum_data_used:", q("select sum(data_used) from vpn_keys"))
    print("alembic_ver  :", q("select version_num from alembic_version"))
except Exception as e:
    print("DB read error:", e)
PY
else
  echo "  !!! vpnbzk.db не найден в volume ($APP_MP) — БД не залита?"
fi

# ── 5. Сертификат ──
sec "5. TLS СЕРТИФИКАТ"
if [ -n "$CRT_MP" ]; then
  echo "  содержимое live/:"; ls "$CRT_MP/live" 2>/dev/null | sed 's/^/    /' || echo "    (пусто — сертификата нет)"
  CERT="$CRT_MP/live/$DOMAIN/fullchain.pem"
  if [ -f "$CERT" ]; then
    kv "cert" "$CERT"
    openssl x509 -noout -subject -enddate -in "$CERT" 2>/dev/null | sed 's/^/    /'
  else
    echo "  !!! Нет fullchain.pem для $DOMAIN → nginx не стартует без сертификата"
  fi
else
  echo "  certbot volume не найден"
fi

# ── 6. XRAY ──
sec "6. XRAY"
XRAY_UP=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -c 'xray')
kv "контейнер xray запущен" "$([ "$XRAY_UP" -ge 1 ] && echo да || echo НЕТ)"
if [ -n "$XCFG_MP" ] && [ -f "$XCFG_MP/config.json" ]; then
  echo "  config.json (inbounds):"
  python3 - "$XCFG_MP/config.json" <<'PY' 2>&1 | sed 's/^/    /'
import json, sys
try:
    c = json.load(open(sys.argv[1]))
    for ib in c.get("inbounds", []):
        cnt = len(ib.get("settings", {}).get("clients", []) or [])
        rs = ib.get("streamSettings", {}).get("realitySettings")
        extra = ""
        if rs: extra = " reality[sni=%s sid=%s]" % (rs.get("serverNames"), rs.get("shortIds"))
        print("- %-14s port=%s clients=%d net=%s%s" % (
            ib.get("tag"), ib.get("port"),
            cnt, ib.get("streamSettings", {}).get("network"), extra))
    print("outbounds:", [o.get("tag") for o in c.get("outbounds", [])])
except Exception as e:
    print("config parse error:", e)
PY
else
  echo "  config.json не найден в $XCFG_MP"
fi
echo "  xray -test:"
$DC exec -T xray xray -test -config /etc/xray/config.json 2>&1 | tail -5 | sed 's/^/    /' || echo "    (xray не запущен)"
echo "  слушающие порты внутри xray:"
$DC exec -T xray sh -c "ss -tlnp 2>/dev/null || netstat -tln 2>/dev/null" 2>&1 | grep -E ':(443|8443|8444|8445|10085)' | sed 's/^/    /' || echo "    (нет данных)"

# ── 7. Сбор статистики трафика (корень проблемы с трафиком) ──
sec "7. XRAY STATS API (учёт трафика)"
echo "  statsquery user>>> (первые строки):"
$DC exec -T ufo-app xray api statsquery --server=xray:10085 '-pattern=user>>>' 2>&1 | head -12 | sed 's/^/    /'
echo "  (если пусто/ошибка — трафик не собирается; если JSON/stat: — ок)"

# ── 8. NGINX ──
sec "8. NGINX"
NGX_UP=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -c 'nginx')
kv "контейнер nginx запущен" "$([ "$NGX_UP" -ge 1 ] && echo да || echo НЕТ)"
echo "  nginx -t:"
$DC exec -T nginx nginx -t 2>&1 | sed 's/^/    /' || echo "    (nginx не запущен)"

# ── 9. HTTP проверки ──
sec "9. HTTP / СВЯЗНОСТЬ"
kv "app /health (внутри app:8000)" "$($DC exec -T ufo-app sh -c "wget -qO- http://127.0.0.1:8000/health 2>/dev/null" 2>/dev/null | head -c 120)"
kv "nginx локально (Host=$DOMAIN)" "$(curl -ks --max-time 5 https://127.0.0.1/health -H "Host: $DOMAIN" 2>/dev/null | head -c 120)"
kv "DNS $DOMAIN резолвится в" "$(getent ahosts "$DOMAIN" 2>/dev/null | awk '{print $1}' | sort -u | tr '\n' ' ')"
kv "снаружи https://$DOMAIN/health" "$(curl -s --max-time 8 "https://$DOMAIN/health" 2>/dev/null | head -c 120)"

# ── 10. Связь с RU и нодами ──
sec "10. СВЯЗНОСТЬ С RU / НОДАМИ (из app-контейнера)"
RU_IP=$(getenv RU_SERVER_IP); RU_PORT=$(getenv RU_TRANSIT_PORT)
if [ -n "$RU_IP" ]; then
  echo "  RU $RU_IP:${RU_PORT:-443}:"
  $DC exec -T ufo-app sh -c "nc -zw3 $RU_IP ${RU_PORT:-443}; echo '    rc='\$?" 2>&1 | sed 's/^/  /'
fi
echo "  серверы из БД (host, api_url, last_sync):"
if [ -n "$APP_MP" ] && [ -f "$APP_MP/vpnbzk.db" ]; then
  python3 - "$APP_MP/vpnbzk.db" <<'PY' 2>&1 | sed 's/^/    /'
import sqlite3, sys
try:
    c = sqlite3.connect("file:%s?mode=ro" % sys.argv[1], uri=True)
    for r in c.execute("select name,host,api_url,region,is_active,last_sync,last_sync_status from servers"):
        print(r)
except Exception as e:
    print("err:", e)
PY
fi

# ── 11. Логи (хвосты) ──
sec "11. ЛОГИ — ufo-app (tail 25)"
$DC logs --tail=25 ufo-app 2>&1 | sed 's/^/  /'
sec "11. ЛОГИ — xray (tail 25)"
$DC logs --tail=25 xray 2>&1 | sed 's/^/  /'
sec "11. ЛОГИ — nginx (tail 15)"
$DC logs --tail=15 nginx 2>&1 | sed 's/^/  /'

# ── 12. Хостовые порты ──
sec "12. ПОРТЫ НА ХОСТЕ (80/443/2053)"
ss -tlnp 2>/dev/null | grep -E ':(80|443|2053)\b' | sed 's/^/  /' || echo "  (ss недоступен)"

sec "КОНЕЦ ОТЧЁТА"
echo "Отправьте весь вывод выше. Секреты замаскированы — можно копировать целиком."
