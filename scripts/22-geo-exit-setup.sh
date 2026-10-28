#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  22-geo-exit-setup.sh — голый xray-выход для Google/Gemini (GEO-EXIT)
# ═══════════════════════════════════════════════════════════════════════════
#  Запускать на ЧИСТОМ сервере-выходе (не на главном). Ставит xray (systemd).
#
#  Прямой режим: выход слушает VLESS REALITY, главный подключается к нему.
#    bash 22-geo-exit-setup.sh [порт]                    (по умолчанию 443)
#    Печатает строки GEO_EXIT_* для .env главного. Ключи и UUID сохраняются.
#
#  Обратный режим (GEO_EXIT_MODE=reverse): выход сам подключается к REALITY
#  главного и держит туннель — когда главный не может открыть TCP к выходу.
#    bash 22-geo-exit-setup.sh reverse <REALITY-IP главного> <PUBLIC_KEY> <SNI> <SHORT_ID> <GEO_EXIT_UUID>
#    Готовую команду печатает на главном:  bash scripts/22-geo-exit-setup.sh print-reverse
# ═══════════════════════════════════════════════════════════════════════════
set -e
CFG=/usr/local/etc/xray/config.json
STATE=/usr/local/etc/xray/geo-exit.env

if [ "$1" = "print-reverse" ]; then
  SELF=$(readlink -f "$0")
  cd "${PROJECT_DIR:-/opt/ufobzk}"
  g() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '"'"'"; }
  # Одна строка для выхода: сам скрипт (gzip+base64) + аргументы из .env
  echo "echo $(gzip -9c "$SELF" | base64 -w0) | base64 -d | gunzip > /root/22-geo-exit-setup.sh &&" \
       "bash /root/22-geo-exit-setup.sh reverse $(g NL_SERVER_IP) $(g REALITY_PUBLIC_KEY)" \
       "$(g REALITY_SERVER_NAMES | cut -d, -f1)" "$(g REALITY_SHORT_ID | cut -d, -f1)" "$(g GEO_EXIT_UUID)"
  exit 0
fi

[ "$(id -u)" = 0 ] || { echo "нужен root"; exit 1; }
command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl; }
if ! command -v xray >/dev/null; then
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
fi
xray version | head -1

if [ "$1" = "reverse" ]; then
  [ $# -eq 6 ] || { echo "нужно 5 аргументов после reverse — см. шапку скрипта"; exit 1; }
  MAIN=$2 PBK=$3 SNI=$4 SID=$5 UUID=$6
  cat > "$CFG" <<EOF
{
  "log": {"loglevel": "warning"},
  "outbounds": [
    {"tag": "DIRECT", "protocol": "freedom"},
    {
      "tag": "TO-MAIN",
      "protocol": "vless",
      "settings": {
        "address": "$MAIN",
        "port": 443,
        "id": "$UUID",
        "encryption": "none",
        "flow": "xtls-rprx-vision",
        "reverse": {"tag": "r-in"}
      },
      "streamSettings": {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
          "serverName": "$SNI",
          "fingerprint": "chrome",
          "publicKey": "$PBK",
          "shortId": "$SID"
        }
      }
    }
  ],
  "routing": {"rules": [{"inboundTag": ["r-in"], "outboundTag": "DIRECT"}]}
}
EOF
  xray run -test -c "$CFG" >/dev/null
  systemctl enable xray >/dev/null 2>&1
  systemctl restart xray
  sleep 2
  systemctl is-active xray
  journalctl -u xray --no-pager -n 5 | grep -viE 'penetrates|unified platform'
  echo
  echo "=== НА ГЛАВНОМ в .env: GEO_EXIT_MODE=reverse и GEO_EXIT_UUID=$UUID ==="
  exit 0
fi

PORT=${1:-443}
SNI=www.samsung.com
if ss -ltnH "sport = :$PORT" | grep -q . && ! ss -ltnpH "sport = :$PORT" | grep -q xray; then
  echo "порт $PORT занят:"; ss -ltnp "sport = :$PORT"; echo "запусти с другим портом: bash $0 8443"; exit 1
fi

if [ -f "$STATE" ]; then
  . "$STATE"
else
  KEYS=$(xray x25519)
  # Формат вывода менялся (Private key/Public key → PrivateKey/Password),
  # но порядок тот же: первая строка — приватный, вторая — публичный.
  PRIV=$(echo "$KEYS" | sed -n 1p | awk '{print $NF}')
  PUB=$(echo "$KEYS" | sed -n 2p | awk '{print $NF}')
  UUID=$(xray uuid)
  SID=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
  printf 'PRIV=%s\nPUB=%s\nUUID=%s\nSID=%s\n' "$PRIV" "$PUB" "$UUID" "$SID" > "$STATE"
  chmod 600 "$STATE"
fi

cat > "$CFG" <<EOF
{
  "log": {"loglevel": "warning"},
  "inbounds": [{
    "tag": "GEO-EXIT-IN",
    "listen": "0.0.0.0",
    "port": $PORT,
    "protocol": "vless",
    "settings": {
      "clients": [{"id": "$UUID", "flow": "xtls-rprx-vision", "email": "main-geo"}],
      "decryption": "none"
    },
    "streamSettings": {
      "network": "tcp",
      "security": "reality",
      "realitySettings": {
        "dest": "$SNI:443",
        "serverNames": ["$SNI"],
        "privateKey": "$PRIV",
        "shortIds": ["$SID"]
      }
    }
  }],
  "outbounds": [
    {"tag": "DIRECT", "protocol": "freedom"}
  ]
}
EOF

xray run -test -c "$CFG" >/dev/null
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow "$PORT"/tcp >/dev/null; fi
systemctl enable xray >/dev/null 2>&1
systemctl restart xray
sleep 1
systemctl is-active xray

IP=$(curl -4 -s -m 8 https://api.ipify.org)
echo
echo "=== ДОБАВИТЬ В /opt/ufobzk/.env НА ГЛАВНОМ ==="
echo "GEO_EXIT_HOST=$IP"
echo "GEO_EXIT_PORT=$PORT"
echo "GEO_EXIT_UUID=$UUID"
echo "GEO_EXIT_PUBLIC_KEY=$PUB"
echo "GEO_EXIT_SHORT_ID=$SID"
echo "GEO_EXIT_SN=$SNI"
