#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  22-geo-exit-setup.sh — голый xray-выход для Google/Gemini (GEO-EXIT)
# ═══════════════════════════════════════════════════════════════════════════
#  Запускать на ЧИСТОМ сервере-выходе (не на главном). Ставит xray (systemd),
#  поднимает VLESS REALITY с одним транзитным клиентом и печатает строки
#  GEO_EXIT_* для .env главного. Повторный запуск сохраняет ключи и UUID.
#
#  Запуск:  bash 22-geo-exit-setup.sh [порт]      (по умолчанию 443)
# ═══════════════════════════════════════════════════════════════════════════
set -e
[ "$(id -u)" = 0 ] || { echo "нужен root"; exit 1; }
PORT=${1:-443}
SNI=www.samsung.com
CFG=/usr/local/etc/xray/config.json
STATE=/usr/local/etc/xray/geo-exit.env

if ss -ltnH "sport = :$PORT" | grep -q . && ! ss -ltnpH "sport = :$PORT" | grep -q xray; then
  echo "порт $PORT занят:"; ss -ltnp "sport = :$PORT"; echo "запусти с другим портом: bash $0 8443"; exit 1
fi

command -v curl >/dev/null || { apt-get update -qq && apt-get install -y -qq curl; }
if ! command -v xray >/dev/null; then
  bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
fi
xray version | head -1

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
    {"tag": "DIRECT", "protocol": "freedom", "settings": {"domainStrategy": "UseIPv4"}}
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
