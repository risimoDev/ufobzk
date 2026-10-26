#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  19-reality-matrix.sh — какая серверная настройка REALITY ломает sing-box
# ═══════════════════════════════════════════════════════════════════════════
#  Поднимает временные REALITY-серверы на 127.0.0.1:2444x с теми же ключами,
#  что в .env, но с разными вариантами настроек, и подключается к каждому
#  клиентами sing-box (ядро Karing/Hiddify) и xray. Боевой стек не трогает.
#
#  Запуск:  cd /opt/ufobzk && bash scripts/19-reality-matrix.sh
# ═══════════════════════════════════════════════════════════════════════════
set +e
cd "${PROJECT_DIR:-/opt/ufobzk}" || exit 1
W=/tmp/ufobzk-rm
rm -rf "$W"; mkdir -p "$W"
docker exec ufobzk-xray cat /etc/xray/config.json > "$W/server.json" || exit 1

python3 - "$W" <<'EOF'
import json, sys, copy
w = sys.argv[1]
c = json.load(open(f'{w}/server.json'))
prod = next(i for i in c['inbounds'] if i['tag'] == 'VLESS-REALITY')
rs0 = prod['streamSettings']['realitySettings']
uid = prod['settings']['clients'][0]['id']
pub = None
for l in open('.env', encoding='utf-8'):
    if l.startswith('REALITY_PUBLIC_KEY='): pub = l.split('=', 1)[1].strip().strip('"\'')
sni, sid = rs0['serverNames'][0], rs0['shortIds'][0]

# S1 = как в бою; S2 = без minClientVer/maxClientVer/maxTimeDiff; S3 = минимальный классический
variants = {}
rs1 = copy.deepcopy(rs0); rs1['show'] = True
variants['S1'] = rs1
rs2 = copy.deepcopy(rs1)
for k in ('minClientVer', 'maxClientVer', 'maxTimeDiff'): rs2.pop(k, None)
variants['S2'] = rs2
variants['S3'] = {"show": True, "dest": rs0['dest'], "xver": 0, "serverNames": rs0['serverNames'],
                  "privateKey": rs0['privateKey'], "shortIds": [sid]}
port = {'S1': 24441, 'S2': 24442, 'S3': 24443}
for name, rs in variants.items():
    cfg = {"log": {"loglevel": "debug"},
           "inbounds": [{"listen": "127.0.0.1", "port": port[name], "protocol": "vless",
                         "settings": {"clients": [{"id": uid, "flow": "xtls-rprx-vision"}], "decryption": "none"},
                         "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": rs}}],
           "outbounds": [{"protocol": "freedom"}]}
    json.dump(cfg, open(f'{w}/{name}.json', 'w'), indent=1)
    print(name, 'realitySettings keys:', sorted(k for k in rs if k != 'privateKey'))

# Каждый сервер дважды: с GODEBUG=tlsmlkem=0 (как в бою) и без (порт +10)
targets = []
for name, p in port.items():
    targets += [(f'{name}-mlkemOFF', p), (f'{name}-mlkemON', p + 10)]
inb, outb, rules = [], [], []
lp = 25000
plan = []
for tname, tp in targets:
    for fp in ('chrome', 'firefox'):
        lp += 1
        tag = f'{tname}-{fp}'
        inb.append({"type": "socks", "tag": f'in-{tag}', "listen": "127.0.0.1", "listen_port": lp})
        outb.append({"type": "vless", "tag": f'out-{tag}', "server": "127.0.0.1", "server_port": tp,
                     "uuid": uid, "flow": "xtls-rprx-vision",
                     "tls": {"enabled": True, "server_name": sni, "utls": {"enabled": True, "fingerprint": fp},
                             "reality": {"enabled": True, "public_key": pub, "short_id": sid}}})
        rules.append({"inbound": [f'in-{tag}'], "outbound": f'out-{tag}'})
        plan.append(f'sb {tag} {lp}')
json.dump({"log": {"level": "warn"}, "inbounds": inb, "outbounds": outb, "route": {"rules": rules}},
          open(f'{w}/sb.json', 'w'), indent=1)
# Контроль: xray-клиент на S1 (оба режима)
for tname, tp in (('S1-mlkemOFF', 24441), ('S1-mlkemON', 24451)):
    lp += 1
    json.dump({"log": {"loglevel": "warning"},
               "inbounds": [{"listen": "127.0.0.1", "port": lp, "protocol": "socks", "settings": {"udp": False}}],
               "outbounds": [{"protocol": "vless", "settings": {"vnext": [{"address": "127.0.0.1", "port": tp,
                   "users": [{"id": uid, "encryption": "none", "flow": "xtls-rprx-vision"}]}]},
                   "streamSettings": {"network": "tcp", "security": "reality", "realitySettings":
                       {"serverName": sni, "fingerprint": "chrome", "publicKey": pub, "shortId": sid}}}]},
              open(f'{w}/xc-{tname}.json', 'w'))
    plan.append(f'xray {tname}-chrome {lp}')
open(f'{w}/plan.txt', 'w').write('\n'.join(plan) + '\n')
EOF

docker rm -f ufobzk-rm-srv ufobzk-rm-sb >/dev/null 2>&1
docker run -d --name ufobzk-rm-srv --network host -v "$W":/w --entrypoint sh teddysun/xray -c '
  for s in S1 S2 S3; do
    GODEBUG=tlsmlkem=0 xray run -c /w/$s.json > /w/srv-$s-mlkemOFF.log 2>&1 &
    sed "s/\"port\": 2444\([0-9]\)/\"port\": 2445\1/" /w/$s.json > /w/$s-on.json
    xray run -c /w/$s-on.json > /w/srv-$s-mlkemON.log 2>&1 &
  done
  for f in /w/xc-*.json; do xray run -c $f > $f.log 2>&1 & done
  wait' >/dev/null
docker run -d --name ufobzk-rm-sb --network host -v "$W":/w ghcr.io/sagernet/sing-box:latest run -c /w/sb.json >/dev/null
sleep 5
docker exec ufobzk-rm-sb sing-box version 2>/dev/null | head -1
docker exec ufobzk-rm-srv xray version 2>/dev/null | head -1

echo; echo "=== МАТРИЦА (OK = handshake прошёл и google открылся) ==="
while read -r kind tag lp; do
  code=$(curl -s -o /dev/null -m 10 -x "socks5h://127.0.0.1:$lp" -w '%{http_code}' https://www.google.com)
  case "$code" in 2*|3*) r=OK ;; *) r="FAIL($code)" ;; esac
  printf '  %-5s %-28s %s\n' "$kind" "$tag" "$r"
done < "$W/plan.txt"

echo; echo "=== sing-box ошибки ==="
docker logs ufobzk-rm-sb 2>&1 | grep -iE 'error|fail' | sed 's/^.*\] //' | sort | uniq -c | head -n 15
echo; echo "=== REALITY debug сервера S1-mlkemOFF (как в бою) ==="
grep -iE 'reality|handshake|clientver|time|shortid|invalid' "$W/srv-S1-mlkemOFF.log" | grep -v Warning | tail -n 15

docker rm -f ufobzk-rm-srv ufobzk-rm-sb >/dev/null 2>&1
echo "=== КОНЕЦ ==="
