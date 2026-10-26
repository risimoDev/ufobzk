#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  18-client-test.sh — сквозная проверка VPN-ссылок С САМОГО главного сервера
# ═══════════════════════════════════════════════════════════════════════════
#  Поднимает временные клиенты (xray и sing-box) в docker --network host,
#  подключается к своим же inbound'ам как обычный пользователь и открывает
#  сайты через каждый. Отдельно проверяет транзит RU-PROXY тем же outbound,
#  что в боевом config.json. Ничего в стеке не меняет.
#
#  Запуск:  cd /opt/ufobzk && bash scripts/18-client-test.sh
# ═══════════════════════════════════════════════════════════════════════════
set +e
cd "${PROJECT_DIR:-/opt/ufobzk}" || exit 1
W=/tmp/ufobzk-ct
rm -rf "$W"; mkdir -p "$W"
docker exec ufobzk-xray cat /etc/xray/config.json > "$W/server.json" || { echo "нет config.json"; exit 1; }

python3 - "$W" <<'EOF'
import json, sys
w = sys.argv[1]
c = json.load(open(f'{w}/server.json'))
env = {}
for l in open('.env', encoding='utf-8'):
    l = l.strip()
    if '=' in l and not l.startswith('#'):
        k, v = l.split('=', 1); env[k] = v.strip('"\'')
uid = next(i for i in c['inbounds'] if i['tag'] == 'VLESS-REALITY')['settings']['clients'][0]['id']
dom = env['DOMAIN']
ip = env.get('NL_SERVER_IP') or env.get('REALITY_BIND_IP')
sni = env['REALITY_SERVER_NAMES'].split(',')[0].strip()
pbk, sid = env['REALITY_PUBLIC_KEY'], env['REALITY_SHORT_ID'].split(',')[0].strip()
print(f'uuid={uid[:8]}…  reality={ip}:443 sni={sni} sid={sid}')

def xc(port, outbound):
    return {"log": {"loglevel": "info"},
            "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks", "settings": {"udp": False}}],
            "outbounds": [outbound]}
def vless(addr, stream, flow=''):
    u = {"id": uid, "encryption": "none"}
    if flow: u["flow"] = flow
    return {"protocol": "vless", "settings": {"vnext": [{"address": addr, "port": 443, "users": [u]}]}, "streamSettings": stream}
tls = lambda alpn: {"serverName": dom, "fingerprint": "chrome", "alpn": alpn}

x = {
  'x-reality': xc(21001, vless(ip, {"network": "tcp", "security": "reality",
        "realitySettings": {"serverName": sni, "fingerprint": "chrome", "publicKey": pbk, "shortId": sid}}, 'xtls-rprx-vision')),
  'x-ws':    xc(21002, vless(dom, {"network": "ws", "security": "tls", "tlsSettings": tls(["http/1.1"]), "wsSettings": {"path": "/vless-ws", "host": dom}})),
  'x-xhttp': xc(21003, vless(dom, {"network": "xhttp", "security": "tls", "tlsSettings": tls(["h2", "http/1.1"]), "xhttpSettings": {"path": "/xhttp", "host": dom, "mode": env.get('XHTTP_MODE') or 'auto'}})),
  'x-grpc':  xc(21004, vless(dom, {"network": "grpc", "security": "tls", "tlsSettings": tls(["h2"]), "grpcSettings": {"serviceName": "VpnService"}})),
}
ru = next((o for o in c['outbounds'] if o.get('tag') == 'RU-PROXY'), None)
if ru:
    x['x-rutransit'] = xc(21006, ru)
for k, v in x.items():
    json.dump(v, open(f'{w}/{k}.json', 'w'), indent=1)

# sing-box = ядро Karing/Hiddify
sb = {"log": {"level": "info"},
      "inbounds": [{"type": "socks", "listen": "127.0.0.1", "listen_port": 21005}],
      "outbounds": [{"type": "vless", "tag": "out", "server": ip, "server_port": 443, "uuid": uid,
                     "flow": "xtls-rprx-vision",
                     "tls": {"enabled": True, "server_name": sni,
                             "utls": {"enabled": True, "fingerprint": "chrome"},
                             "reality": {"enabled": True, "public_key": pbk, "short_id": sid}}}]}
json.dump(sb, open(f'{w}/sb-reality.json', 'w'), indent=1)
EOF

docker rm -f ufobzk-ct-xray ufobzk-ct-sb >/dev/null 2>&1
docker run -d --name ufobzk-ct-xray --network host -v "$W":/ct --entrypoint sh teddysun/xray \
  -c 'for f in /ct/x-*.json; do n=$(basename $f .json); xray run -c $f > /ct/$n.log 2>&1 & done; wait' >/dev/null
docker run -d --name ufobzk-ct-sb --network host -v "$W":/ct ghcr.io/sagernet/sing-box:latest \
  run -c /ct/sb-reality.json >/dev/null 2>"$W/sb-pull.err" || echo "sing-box не запустился: $(tail -n2 $W/sb-pull.err)"
sleep 5

t() { printf '  %-12s %-26s ' "$1" "$3"; curl -s -o /dev/null -m 12 -x "socks5h://127.0.0.1:$2" -w '%{http_code} %{time_total}s\n' "$3" || echo FAIL; }
echo; echo "=== РЕЗУЛЬТАТ (200/301/302 = работает, 000/FAIL = нет) ==="
for p in x-reality:21001 sb-reality:21005 x-ws:21002 x-xhttp:21003 x-grpc:21004; do
  n=${p%:*}; port=${p#*:}
  for u in https://www.google.com https://www.instagram.com https://gemini.google.com https://ya.ru; do t "$n" "$port" "$u"; done
done
echo "  --- транзит RU-PROXY напрямую (тот же outbound, что в бою):"
for u in https://ya.ru https://www.gosuslugi.ru https://ipinfo.io/ip; do t rutransit 21006 "$u"; done
printf '  RU-выход IP: '; curl -s -m 12 -x socks5h://127.0.0.1:21006 https://ipinfo.io/ip; echo
printf '  NL-выход IP (через REALITY): '; curl -s -m 12 -x socks5h://127.0.0.1:21001 https://ipinfo.io/ip; echo
printf '  страна для Google (через WS): '; curl -s -m 12 -x socks5h://127.0.0.1:21002 -o /dev/null -w '%{redirect_url}\n' https://www.google.com/ncr

echo; echo "=== ЛОГИ КЛИЕНТОВ (ошибки) ==="
docker logs ufobzk-ct-sb > "$W/sb-reality.log" 2>&1
for f in "$W"/*.log; do
  echo "--- $(basename $f)"; grep -viE 'accepted|^Xray|anti-censorship|Reading config|deprecated' "$f" | tail -n 6
done
echo; echo "=== СЕРВЕРНЫЙ XRAY: REALITY / ошибки за последнюю минуту ==="
docker logs --since 1m ufobzk-xray 2>&1 | grep -iE 'reality|fail|error|reject' | grep -v X-Forwarded-For | tail -n 15
docker exec ufobzk-xray sh -c 'tail -n 400 /var/log/xray/error.log' | grep -iE 'RU-PROXY|reality|194\.226' | tail -n 10

docker rm -f ufobzk-ct-xray ufobzk-ct-sb >/dev/null 2>&1
echo "=== КОНЕЦ ==="
