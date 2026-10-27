#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  21-geo-nodes.sh — какую страну Google видит через каждую ноду из БД
# ═══════════════════════════════════════════════════════════════════════════
#  Кандидаты в GEO-EXIT (GEO_NODE_NAME). Берёт ВСЕ ноды из таблицы servers,
#  включая отключённые, поднимает временный xray-клиент на каждую и спрашивает
#  страну у Google. UUID — первый клиент REALITY главного (на нодах те же
#  пользователи). Боевой стек не трогает.
#
#  Запуск:  cd /opt/ufobzk && bash scripts/21-geo-nodes.sh
# ═══════════════════════════════════════════════════════════════════════════
set +e
cd "${PROJECT_DIR:-/opt/ufobzk}" || exit 1
W=/tmp/ufobzk-gn
rm -rf "$W"; mkdir -p "$W"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

docker exec ufobzk-app python -c "
import json
from app.models import SessionLocal, Server
db = SessionLocal()
print(json.dumps([dict(name=s.name, host=s.host, port=s.reality_port, active=bool(s.is_active),
    pbk=s.reality_public_key, sid=s.reality_short_id, sni=(s.reality_server_names or 'www.samsung.com').split(',')[0].strip())
    for s in db.query(Server).all()]))
" > "$W/nodes.json" || { echo "не прочитал servers из БД"; exit 1; }
docker exec ufobzk-xray cat /etc/xray/config.json > "$W/server.json"

python3 - "$W" <<'EOF'
import json, sys
w = sys.argv[1]
nodes = json.load(open(f'{w}/nodes.json'))
c = json.load(open(f'{w}/server.json'))
uid = next(i for i in c['inbounds'] if i['tag'] == 'VLESS-REALITY')['settings']['clients'][0]['id']
plan = []
for n, s in enumerate(nodes):
    if not (s['pbk'] and s['sid']):
        print(f"  {s['name']}: нет ключей REALITY — пропуск"); continue
    lp = 22100 + n
    json.dump({"log": {"loglevel": "warning"},
               "inbounds": [{"listen": "127.0.0.1", "port": lp, "protocol": "socks", "settings": {"udp": False}}],
               "outbounds": [{"protocol": "vless", "settings": {"vnext": [{"address": s['host'], "port": s['port'],
                   "users": [{"id": uid, "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
                   "streamSettings": {"network": "tcp", "security": "reality", "realitySettings":
                       {"serverName": s['sni'], "fingerprint": "chrome", "publicKey": s['pbk'], "shortId": s['sid']}}}]},
              open(f'{w}/n{n}.json', 'w'))
    plan.append(f"{lp}\t{s['name']}\t{s['host']}:{s['port']}\t{'активна' if s['active'] else 'отключена'}")
open(f'{w}/plan.txt', 'w').write('\n'.join(plan) + '\n')
EOF

docker rm -f ufobzk-gn >/dev/null 2>&1
docker run -d --name ufobzk-gn --network host -v "$W":/w --entrypoint sh teddysun/xray:26.5.9 \
  -c 'for f in /w/n*.json; do xray run -c $f > $f.log 2>&1 & done; wait' >/dev/null
sleep 4

echo; echo "=== НОДЫ: страна для Google / внешний IP ==="
while IFS=$'\t' read -r lp name addr state; do
  [ -z "$lp" ] && continue
  ip=$(curl -s -m 10 -x "socks5h://127.0.0.1:$lp" https://api.ipify.org)
  g=$(curl -s -m 15 -A "$UA" -x "socks5h://127.0.0.1:$lp" https://www.google.com | sed -n 's/.*"[a-z]\{2\}_\([A-Z]\{2\}\)".*/\1/p' | head -1)
  y=$(curl -s -m 15 -A "$UA" -x "socks5h://127.0.0.1:$lp" https://www.youtube.com | grep -o '"countryCode":"[A-Z]*"' | head -1 | cut -d'"' -f4)
  printf '  %-16s %-22s %-10s IP=%-16s Google=%-4s YouTube=%s\n' "$name" "$addr" "$state" "${ip:-НЕТ}" "${g:-?}" "${y:-?}"
done < "$W/plan.txt"

echo; echo "=== ошибки клиентов ==="
for f in "$W"/n*.json.log; do grep -iE 'fail|error' "$f" | tail -n 2; done
docker rm -f ufobzk-gn >/dev/null 2>&1
echo "=== КОНЕЦ ==="
