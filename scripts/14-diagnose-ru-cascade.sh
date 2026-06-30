#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  14-diagnose-ru-cascade.sh — Полная диагностика каскада НА RU-сервере
# ═══════════════════════════════════════════════════════════════════════════
#  Запускать НА RU-сервере (xray bare-metal/systemd). ТОЛЬКО ЧТЕНИЕ.
#
#  Проверяет ОБА направления каскада:
#    • NL→RU (вход): NL шлёт .ru-трафик в REALITY-inbound RU под транзитным UUID
#    • RU→NL (возврат): RU гонит НЕ-.ru трафик обратно на NL через NL-PROXY outbound
#
#  Запуск (для авто-сверки передайте параметры с NL-сервера):
#    bash 14-diagnose-ru-cascade.sh <NL_IP> <RU_TRANSIT_UUID> <NL_REALITY_PUBLIC_KEY>
#  Пример:
#    bash 14-diagnose-ru-cascade.sh 31.56.180.197 4da8a005-5078-432d-829d-3c4fb61ecbd5 NG63mjKr8RNFKeJSEyoqOBW_UwPos8YGpqbkfamc60M
#  Без параметров — просто печатает все значения для ручной сверки.
# ═══════════════════════════════════════════════════════════════════════════

set +e

CFG="${XRAY_CONFIG:-/etc/xray/config.json}"
NL_IP="${1:-}"
TRANSIT_UUID="${2:-}"
NL_PBK="${3:-}"

XRAY_BIN="$(command -v xray || echo /usr/local/bin/xray)"

sec() { echo; echo "═══════════════════════════════════════════════════════════"; echo "### $* ###"; echo "═══════════════════════════════════════════════════════════"; }

echo "RU CASCADE DIAGNOSTIC"
echo "generated: $(date -u '+%Y-%m-%d %H:%M:%S') UTC"
echo "config: $CFG"
echo "ожидаем (если переданы): NL_IP=$NL_IP TRANSIT_UUID=$TRANSIT_UUID NL_PBK=$NL_PBK"

# ── 0. Система / сервис ──
sec "0. СИСТЕМА И СЕРВИС XRAY"
echo "  hostname    : $(hostname)"
echo "  egress IP   : $(curl -4 -s --max-time 5 ifconfig.me 2>/dev/null || echo '?')"
echo "  xray bin    : $XRAY_BIN"
echo "  xray version: $($XRAY_BIN version 2>/dev/null | head -1)"
echo "  systemd xray: $(systemctl is-active xray 2>/dev/null) / $(systemctl is-enabled xray 2>/dev/null)"
echo "  слушающие порты (443/2053/8443):"
ss -tlnp 2>/dev/null | grep -E ':(443|2053|8443)\b' | sed 's/^/    /' || echo "    (нет данных)"

# ── 1. Валидность конфига ──
sec "1. ПРОВЕРКА КОНФИГА (xray -test)"
if [ -f "$CFG" ]; then
  $XRAY_BIN -test -config "$CFG" 2>&1 | tail -3 | sed 's/^/  /' \
    || $XRAY_BIN run -test -config "$CFG" 2>&1 | tail -3 | sed 's/^/  /'
else
  echo "  !!! $CFG не найден"
fi

# ── 2-4. Разбор конфига: inbound + outbound + routing + сверки ──
sec "2. РАЗБОР КОНФИГА И СВЕРКА"
if [ -f "$CFG" ]; then
python3 - "$CFG" "$NL_IP" "$TRANSIT_UUID" "$NL_PBK" <<'PY' 2>&1 | sed 's/^/  /'
import json, sys
cfg, nl_ip, tuuid, nl_pbk = (sys.argv + ["", "", "", ""])[1:5]
try:
    c = json.load(open(cfg))
except Exception as e:
    print("config parse error:", e); sys.exit(0)

# --- REALITY inbound (вход NL→RU + прямые клиенты) ---
rin = None
for ib in c.get("inbounds", []):
    rs = ib.get("streamSettings", {}).get("realitySettings")
    if rs and ib.get("protocol") == "vless":
        rin = ib; break

print("== REALITY INBOUND (приём NL→RU и прямых клиентов) ==")
priv = ""
if rin:
    rs = rin["streamSettings"]["realitySettings"]
    priv = rs.get("privateKey", "")
    clients = [x.get("id") for x in rin["settings"].get("clients", [])]
    print("  port       :", rin.get("port"))
    print("  serverNames:", rs.get("serverNames"))
    print("  shortIds   :", rs.get("shortIds"))
    print("  privateKey :", priv)
    print("  clients    : %d шт" % len(clients))
    for cid in clients:
        print("     -", cid)
    if tuuid:
        ok = tuuid in clients
        print("  >> транзитный UUID в clients: %s" % (
            "ДА ✓" if ok else "НЕТ ✗ — NL→RU НЕ РАБОТАЕТ (RU отвергает транзит)"))
else:
    print("  !!! REALITY inbound не найден — приём с NL невозможен")

# --- NL-PROXY outbound (возврат RU→NL) ---
print()
print("== NL-PROXY OUTBOUND (возврат RU→NL для не-RU трафика) ==")
nlp = None
for o in c.get("outbounds", []):
    if o.get("protocol") == "vless" and o.get("settings", {}).get("vnext"):
        nlp = o
        if str(o.get("tag", "")).upper().startswith("NL"):
            break
if nlp:
    v = nlp["settings"]["vnext"][0]
    rs = nlp.get("streamSettings", {}).get("realitySettings", {})
    addr = v.get("address"); pbk = rs.get("publicKey")
    print("  tag        :", nlp.get("tag"))
    print("  address    : %s:%s" % (addr, v.get("port")))
    print("  uuid       :", v["users"][0].get("id"))
    print("  flow       :", v["users"][0].get("flow"))
    print("  serverName :", rs.get("serverName"))
    print("  publicKey  :", pbk)
    print("  shortId    :", rs.get("shortId"))
    if nl_ip:
        print("  >> address == новый NL IP (%s): %s" % (
            nl_ip, "ДА ✓" if addr == nl_ip else "НЕТ ✗ — обнови address в NL-PROXY!"))
    if nl_pbk:
        print("  >> publicKey == NL REALITY pub: %s" % (
            "ДА ✓" if pbk == nl_pbk else "НЕТ ✗ — ключ не совпадает с NL"))
else:
    print("  !!! NL-PROXY (vless outbound) НЕ найден — возврат на NL не настроен")

# --- routing ---
print()
print("== ROUTING (куда уходит трафик) ==")
for r in c.get("routing", {}).get("rules", []):
    sel = r.get("domain") or r.get("ip") or r.get("network") or r.get("inboundTag") or "?"
    print("  -> %-10s : %s" % (r.get("outboundTag"), str(sel)[:80]))

# подсказка по публичному ключу RU (для сверки с NL .env RU_TRANSIT_PUBLIC_KEY)
if priv:
    print()
    print("== СВЕРКА: публичный ключ RU-инбаунда из privateKey ==")
    print("  выполните:  %s x25519 -i %s" % ("xray", priv))
    print("  Password(PublicKey) должен == RU_TRANSIT_PUBLIC_KEY в .env на NL")
PY
else
  echo "  $CFG не найден"
fi

# ── 5. Публичный ключ RU из приватного ──
sec "5. ПУБЛИЧНЫЙ КЛЮЧ RU (из privateKey инбаунда)"
PRIV=$(python3 -c "import json;print(json.load(open('$CFG'))['inbounds'][[i for i,b in enumerate(json.load(open('$CFG'))['inbounds']) if b.get('streamSettings',{}).get('realitySettings')][0]]['streamSettings']['realitySettings']['privateKey'])" 2>/dev/null)
if [ -n "$PRIV" ]; then
  $XRAY_BIN x25519 -i "$PRIV" 2>&1 | sed 's/^/  /'
  echo "  ^ Password(PublicKey) должен совпадать с RU_TRANSIT_PUBLIC_KEY в .env на NL"
else
  echo "  (не удалось извлечь privateKey)"
fi

# ── 6. Связность RU → NL (возврат каскада) ──
sec "6. СВЯЗНОСТЬ RU → NL (порт 443 REALITY)"
TARGET="$NL_IP"
[ -z "$TARGET" ] && TARGET=$(python3 -c "import json;[print(o['settings']['vnext'][0]['address']) for o in json.load(open('$CFG')).get('outbounds',[]) if o.get('protocol')=='vless' and o.get('settings',{}).get('vnext')]" 2>/dev/null | head -1)
if [ -n "$TARGET" ]; then
  echo "  цель: $TARGET:443"
  if timeout 3 bash -c "cat </dev/null >/dev/tcp/$TARGET/443" 2>/dev/null; then
    echo "  RU→NL TCP 443: OPEN ✓"
  else
    echo "  RU→NL TCP 443: FAIL ✗ — RU не достаёт до NL (firewall NL / неверный IP / NL down)"
  fi
else
  echo "  цель NL не определена (передайте NL_IP первым аргументом)"
fi

# ── 7. Firewall ──
sec "7. FIREWALL (RU)"
ufw status 2>/dev/null | grep -E '443|2053|Status' | sed 's/^/  /' || echo "  (ufw недоступен/выключен)"

# ── 8. Egress / маршрут по умолчанию ──
sec "8. ВЫХОД В ИНТЕРНЕТ С RU"
echo "  RU egress IP (как видят сайты при DIRECT): $(curl -4 -s --max-time 5 ifconfig.me 2>/dev/null || echo '?')"
echo "  DNS-резолв samsung (для REALITY dest): $(getent ahosts www.samsung.com 2>/dev/null | awk 'NR==1{print $1}')"

# ── 9. Логи ──
sec "9. ЛОГИ XRAY (ошибки)"
LOG=/var/log/xray/error.log
if [ -f "$LOG" ]; then
  echo "  последние строки error.log:"
  tail -20 "$LOG" 2>/dev/null | sed 's/^/    /'
  echo "  --- греп по reality/EOF/rejected/invalid:"
  grep -iE "reality|EOF|rejected|invalid|unauthenticated|failed" "$LOG" 2>/dev/null | tail -15 | sed 's/^/    /'
else
  echo "  $LOG не найден"
  echo "  попробуйте: journalctl -u xray -n 30 --no-pager"
fi

sec "ИТОГ — что проверить"
cat <<'TXT'
  NL→RU работает, если:
    • транзитный UUID есть в clients REALITY-инбаунда (раздел 2)
    • RU_TRANSIT_PUBLIC_KEY на NL == PublicKey из раздела 5
    • RU_TRANSIT_SHORT_ID на NL входит в shortIds, RU_TRANSIT_SN входит в serverNames
  RU→NL (возврат) работает, если:
    • NL-PROXY address == текущий NL IP (раздел 2), TCP до NL:443 OPEN (раздел 6)
    • NL-PROXY publicKey == REALITY_PUBLIC_KEY на NL
  Если что-то ✗ — это и есть причина. Отправьте весь вывод.
TXT
