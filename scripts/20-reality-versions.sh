#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  20-reality-versions.sh — с какой версией Xray-сервера работает sing-box
# ═══════════════════════════════════════════════════════════════════════════
#  Берёт минимальный REALITY-конфиг из 19-reality-matrix.sh (/tmp/ufobzk-rm/S3.json),
#  поднимает его на 127.0.0.1:246xx в разных версиях teddysun/xray и
#  подключается к каждой sing-box'ом (ядро Karing) и xray-клиентом.
#  Боевой стек не трогает. Сначала запустить scripts/19-reality-matrix.sh.
#
#  Запуск:  bash scripts/20-reality-versions.sh [версия ...]
# ═══════════════════════════════════════════════════════════════════════════
set +e
SRC=/tmp/ufobzk-rm
W=/tmp/ufobzk-rv
[ -f "$SRC/S3.json" ] && [ -f "$SRC/sb.json" ] || { echo "сначала bash scripts/19-reality-matrix.sh"; exit 1; }
rm -rf "$W"; mkdir -p "$W"
VERS=${*:-"26.10.21 26.10.17 26.10.10 26.9.30 26.7.28 26.5.9 26.3.27 26.3.23 26.1.23 25.12.8 25.10.15"}

python3 - "$SRC" "$W" $VERS <<'EOF'
import json, sys
src, w, vers = sys.argv[1], sys.argv[2], sys.argv[3:]
s3 = json.load(open(f'{src}/S3.json'))
ob = json.load(open(f'{src}/sb.json'))['outbounds'][0]   # шаблон sing-box (chrome)
inb, outb, rules, plan = [], [], [], []
for n, v in enumerate(vers):
    port = 24600 + n
    cfg = json.loads(json.dumps(s3))
    cfg['inbounds'][0]['port'] = port
    cfg['log']['loglevel'] = 'debug' if n == 0 else 'warning'
    cfg['inbounds'][0]['streamSettings']['realitySettings']['show'] = (n == 0)
    json.dump(cfg, open(f'{w}/srv-{v}.json', 'w'))
    o = json.loads(json.dumps(ob)); o['tag'] = f'o{n}'; o['server_port'] = port
    o['tls']['utls']['fingerprint'] = 'chrome'
    inb.append({"type": "socks", "tag": f'i{n}', "listen": "127.0.0.1", "listen_port": 26000 + n})
    outb.append(o); rules.append({"inbound": [f'i{n}'], "outbound": f'o{n}'})
    plan.append(f'{v} {port} {26000 + n}')
json.dump({"log": {"level": "warn"}, "inbounds": inb, "outbounds": outb, "route": {"rules": rules}},
          open(f'{w}/sb.json', 'w'))
open(f'{w}/plan.txt', 'w').write('\n'.join(plan) + '\n')
EOF

echo "Скачиваю образы (первый раз может занять минуту)..."
while read -r v port lp; do
  docker pull -q "teddysun/xray:$v" >/dev/null 2>&1 || { echo "  нет образа $v"; continue; }
  docker rm -f "ufobzk-rv-$v" >/dev/null 2>&1
  docker run -d --name "ufobzk-rv-$v" --network host -v "$W":/w --entrypoint xray "teddysun/xray:$v" \
    run -c "/w/srv-$v.json" >/dev/null 2>&1
done < "$W/plan.txt"
docker rm -f ufobzk-rv-sb >/dev/null 2>&1
docker run -d --name ufobzk-rv-sb --network host -v "$W":/w ghcr.io/sagernet/sing-box:latest run -c /w/sb.json >/dev/null
sleep 5

echo; echo "=== sing-box $(docker exec ufobzk-rv-sb sing-box version 2>/dev/null | head -1 | awk '{print $3}') -> Xray-сервер ==="
while read -r v port lp; do
  code=$(curl -s -o /dev/null -m 10 -x "socks5h://127.0.0.1:$lp" -w '%{http_code}' https://www.google.com)
  case "$code" in 2*|3*) r=OK ;; *) r="FAIL($code)" ;; esac
  printf '  xray %-10s %s\n' "$v" "$r"
done < "$W/plan.txt"

FIRST=$(head -1 "$W/plan.txt" | awk '{print $1}')
echo; echo "=== debug REALITY сервера $FIRST на подключение sing-box ==="
docker logs "ufobzk-rv-$FIRST" 2>&1 | grep REALITY | head -n 25

docker ps -a --format '{{.Names}}' | grep '^ufobzk-rv-' | xargs -r docker rm -f >/dev/null 2>&1
echo "=== КОНЕЦ ==="
