#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  add-ru-key.sh — Создание временного VLESS-REALITY ключа на RU-сервере
# ═══════════════════════════════════════════════════════════════════════════
#  Запускается НА RU-СЕРВЕРЕ от root:
#    sudo bash add-ru-key.sh [дни, по умолчанию 15] [имя, по умолчанию friend]
#
#  Пример:
#    sudo bash add-ru-key.sh 15 friend
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

DAYS="${1:-15}"
NAME="${2:-friend}"
TAG="temp-${NAME}-$(date +%s)"

CFG="/etc/xray/config.json"
[ -f "$CFG" ] || CFG="/usr/local/etc/xray/config.json"

if [ ! -f "$CFG" ]; then
    echo -e "${RED}❌ Ошибка: Конфиг Xray не найден (/etc/xray/config.json)${NC}" >&2
    exit 1
fi

XRAY_BIN="$(command -v xray || echo /usr/local/bin/xray)"
if [ ! -x "$XRAY_BIN" ]; then
    echo -e "${RED}❌ Ошибка: бинарник xray не найден${NC}" >&2
    exit 1
fi

PY_RES=$(python3 - "$CFG" "$DAYS" "$NAME" "$TAG" "$XRAY_BIN" <<'PYEOF'
import json, sys, os, subprocess, uuid, datetime

cfg_path, days_str, name, tag, xray_bin = sys.argv[1:6]
days = int(days_str)

with open(cfg_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

rin = None
for ib in cfg.get("inbounds", []):
    rs = ib.get("streamSettings", {}).get("realitySettings")
    if rs and ib.get("protocol") == "vless":
        rin = ib
        break

if not rin:
    print("ERR: В конфиге не найден VLESS-REALITY inbound!", file=sys.stderr)
    sys.exit(1)

rs = rin["streamSettings"]["realitySettings"]
priv_key = rs.get("privateKey", "")
short_id = (rs.get("shortIds") or [""])[0]
server_name = (rs.get("serverNames") or ["www.samsung.com"])[0]
port = rin.get("port", 443)

if not priv_key:
    print("ERR: Не найден privateKey в realitySettings!", file=sys.stderr)
    sys.exit(1)

# Вычисляем Public Key
try:
    res = subprocess.run([xray_bin, "x25519", "-i", priv_key], capture_output=True, text=True, check=True)
    pub_key = ""
    for line in res.stdout.splitlines():
        if any(w in line.lower() for w in ["public", "password"]):
            pub_key = line.strip().split()[-1]
            break
except Exception as e:
    print(f"ERR: Ошибка вычисления public key: {e}", file=sys.stderr)
    sys.exit(1)

if not pub_key:
    print("ERR: Не удалось извлечь public key!", file=sys.stderr)
    sys.exit(1)

# Добавляем параметры совместимости с мобильными клиентами (Happ, v2rayNG, Streisand)
rs["minClientVer"] = "1.0.0"
rs["maxClientVer"] = ""
rs["maxTimeDiff"] = 0
sids = [s for s in rs.get("shortIds", []) if s]
if "" not in sids:
    sids.append("")
rs["shortIds"] = sids

# Создаём нового клиента
new_uuid = str(uuid.uuid4())
expire_dt = datetime.datetime.now() + datetime.timedelta(days=days)
expire_str = expire_dt.strftime("%Y-%m-%d %H:%M")

client_obj = {
    "id": new_uuid,
    "flow": "xtls-rprx-vision",
    "email": f"{tag}_exp_{expire_dt.strftime('%Y%m%d_%H%M')}"
}

if "clients" not in rin["settings"]:
    rin["settings"]["clients"] = []

rin["settings"]["clients"].append(client_obj)

# Бэкап
bak_path = cfg_path + f".bak.{int(datetime.datetime.now().timestamp())}"
with open(bak_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)

# Запись нового конфига
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)

if os.path.exists("/usr/local/etc/xray/config.json") and cfg_path != "/usr/local/etc/xray/config.json":
    try:
        with open("/usr/local/etc/xray/config.json", "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

# Получаем внешний IP сервера
ip = ""
for cmd in [["curl", "-4", "-s", "--max-time", "4", "https://ifconfig.me"],
            ["curl", "-4", "-s", "--max-time", "4", "https://api.ipify.org"]]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
        if out and "." in out:
            ip = out
            break
    except Exception:
        pass

if not ip:
    ip = "IP_СЕРВЕРА"

remark = f"RU-{name}-{days}d"
link = f"vless://{new_uuid}@{ip}:{port}?type=tcp&security=reality&pbk={pub_key}&fp=chrome&sni={server_name}&sid={short_id}&flow=xtls-rprx-vision#{remark}"

print(f"UUID={new_uuid}")
print(f"EXPIRE={expire_str}")
print(f"LINK={link}")
print(f"EMAIL={client_obj['email']}")
PYEOF
)

# Проверяем конфиг Xray
if ! "$XRAY_BIN" run -test -config "$CFG" &>/dev/null && ! "$XRAY_BIN" -test -config "$CFG" &>/dev/null; then
    echo -e "${RED}❌ Ошибка валидации нового конфига Xray! Откатываем...${NC}" >&2
    LATEST_BAK=$(ls -t "${CFG}.bak."* 2>/dev/null | head -1)
    if [ -n "$LATEST_BAK" ]; then
        cp "$LATEST_BAK" "$CFG"
    fi
    exit 1
fi

systemctl restart xray

# Парсим переменные из вывода Python
eval "$PY_RES"

# Создаём задачу авто-удаления по истечении срока в cron.d
EXP_SCRIPT="/usr/local/bin/del-key-${TAG}.sh"
cat > "$EXP_SCRIPT" <<CLEANEOF
#!/usr/bin/env bash
python3 -c "import json, subprocess; c=json.load(open('$CFG')); [ib['settings'].__setitem__('clients', [cl for cl in ib.get('settings',{}).get('clients',[]) if '$TAG' not in cl.get('email','')]) for ib in c.get('inbounds',[]) if ib.get('protocol')=='vless']; json.dump(c, open('$CFG','w'), indent=2); subprocess.run(['systemctl','restart','xray'])"
rm -f "/etc/cron.d/expire-${TAG}" "$EXP_SCRIPT"
CLEANEOF
chmod +x "$EXP_SCRIPT"

CRON_TIME=$(date -d "+${DAYS} days" "+%M %H %d %m *")
echo "$CRON_TIME root $EXP_SCRIPT" > "/etc/cron.d/expire-${TAG}"
chmod 644 "/etc/cron.d/expire-${TAG}"

echo ""
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✓ Временный ключ на ${DAYS} дней успешно создан на RU-сервере!${NC}"
echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}Имя/Метка:${NC}      ${NAME}"
echo -e "  ${BOLD}Срок действия:${NC}  до ${EXPIRE} (автоматически удалится)"
echo -e "  ${BOLD}UUID клиента:${NC}   ${UUID}"
echo ""
echo -e "${CYAN}── Ссылка для подключения (отправьте человеку): ──${NC}"
echo -e "${YELLOW}${LINK}${NC}"
echo ""

if command -v qrencode &>/dev/null; then
    echo -e "${CYAN}── QR-код для сканирования телефоном (Happ / v2rayNG / Streisand): ──${NC}"
    qrencode -t ANSIUTF8 "${LINK}"
    echo ""
fi

echo -e "${CYAN}── Если потребуется удалить ключ вручную раньше срока: ──${NC}"
echo -e "  bash ${EXP_SCRIPT}"
echo ""
