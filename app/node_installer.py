"""Генератор скриптов 1-click установки и управления дочерними узлами (нодами)."""

import logging
from typing import Any

from app.models import Server

logger = logging.getLogger(__name__)


def generate_install_script(server: Server, master_base_url: str) -> str:
    """Сгенерировать автономный bash-скрипт для установки ноды в одну команду."""
    reality_sn = (server.reality_server_names or "github.com").split(",")[0].strip()
    quic_sn = server.hysteria_sni or "bing.com"

    reality_p = server.reality_port or 443
    hy_p = server.hysteria_port or 8443
    tuic_p = server.tuic_port or 8444
    host_val = server.host if server.host != "pending" else ""

    script = f"""#!/usr/bin/env bash
# ==============================================================================
#  UFOBZK VPN Node — 1-Click Auto Installer
#  Server Name: {server.name} ({server.region})
#  Role:        {server.role}
# ==============================================================================
set -euo pipefail

RED='\\033[0;31m'; GREEN='\\033[0;32m'; YELLOW='\\033[1;33m'; CYAN='\\033[0;36m'; BOLD='\\033[1m'; NC='\\033[0m'
log()  {{ echo -e "${{CYAN}}[•]${{NC}} $*"; }}
ok()   {{ echo -e "${{GREEN}}[✓]${{NC}} $*"; }}
warn() {{ echo -e "${{YELLOW}}[!]${{NC}} $*"; }}
err()  {{ echo -e "${{RED}}[✗]${{NC}} $*" >&2; }}

[ "$(id -u)" -eq 0 ] || {{ err "Please run as root (sudo bash)"; exit 1; }}

INSTALL_TOKEN="{server.install_token}"
API_TOKEN="{server.api_token}"
MASTER_URL="{master_base_url}"
NODE_NAME="{server.name}"
REGION="{server.region}"
ROLE="{server.role}"
REALITY_PORT="{reality_p}"
HYSTERIA_PORT="{hy_p}"
TUIC_PORT="{tuic_p}"
API_PORT="9090"
REALITY_SNI="{reality_sn}"
QUIC_SNI="{quic_sn}"

echo ""
echo -e "${{CYAN}}====================================================${{NC}}"
echo -e "${{CYAN}}  UFOBZK 1-Click Node Installer${{NC}}"
echo -e "${{CYAN}}  Node: ${{NODE_NAME}} [${{REGION}}] | Role: ${{ROLE}}${{NC}}"
echo -e "${{CYAN}}====================================================${{NC}}"
echo ""

# 1. Base tools
log "Checking required system packages..."
command -v curl &>/dev/null || (apt-get update -qq && apt-get install -y -qq curl openssl jq iptables) || true

# 2. Docker installation
if ! command -v docker &>/dev/null; then
    log "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
    ok "Docker installed"
else
    ok "Docker already present"
fi

if docker compose version &>/dev/null; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
else
    log "Installing docker compose plugin..."
    apt-get update -qq && apt-get install -y -qq docker-compose-plugin || true
    DC="docker compose"
fi

# 3. Detect Node Public IP
log "Detecting external IP..."
NODE_IP="$(curl -s4 --max-time 5 https://ifconfig.me || curl -s4 --max-time 5 https://api.ipify.org || curl -s4 --max-time 5 https://icanhazip.com || echo "")"
if [ -z "$NODE_IP" ]; then
    NODE_IP="{host_val}"
fi
[ -n "$NODE_IP" ] || {{ err "Could not detect node external IP. Ensure internet connectivity."; exit 1; }}
ok "Node external IP: $NODE_IP"

# 4. Working directory
WORK_DIR="/opt/ufobzk-node"
mkdir -p "$WORK_DIR/config" "$WORK_DIR/api"
cd "$WORK_DIR"

# 5. Generate Reality Keys
log "Generating REALITY x25519 keypair..."
KEY_OUT="$(docker run --rm ghcr.io/sagernet/sing-box:latest generate reality-keypair 2>/dev/null || docker run --rm teddysun/xray:latest xray x25519 2>/dev/null || echo "")"
PRIVATE_KEY="$(echo "$KEY_OUT" | grep -i 'private' | awk '{{print $NF}}')"
PUBLIC_KEY="$(echo "$KEY_OUT" | grep -iE '^(public|password)' | awk '{{print $NF}}')"
SHORT_ID="$(openssl rand -hex 8)"

if [ -z "$PRIVATE_KEY" ] || [ -z "$PUBLIC_KEY" ]; then
    err "Failed to generate REALITY keypair automatically."
    echo "$KEY_OUT"
    exit 1
fi
ok "Reality keys generated (short_id: $SHORT_ID)"

# 6. Generate Self-signed TLS Cert for Hysteria 2 / TUIC
log "Generating TLS certificate for Hysteria 2 & TUIC (SNI: $QUIC_SNI)..."
openssl req -x509 -newkey rsa:2048 -nodes \\
    -keyout "$WORK_DIR/config/cert.key" \\
    -out "$WORK_DIR/config/cert.crt" \\
    -days 3650 \\
    -subj "/CN=$QUIC_SNI" 2>/dev/null
chmod 600 "$WORK_DIR/config/cert.key"
ok "TLS certificate ready"

# 7. Write lightweight Node API (main.py)
log "Writing Node Agent API..."
cat << 'AGENT_EOF' > "$WORK_DIR/api/main.py"
import http.client
import http.server
import json
import logging
import os
import socket
import urllib.parse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("node-agent")

TOKEN = os.getenv("XRAY_NODE_TOKEN", "")
CONFIG_PATH = os.getenv("XRAY_CONFIG_PATH", "/etc/sing-box/config.json")
CONTAINER = os.getenv("XRAY_CONTAINER_NAME", "ufobzk-node-core")
DOCKER_SOCK = "/var/run/docker.sock"
PORT = int(os.getenv("NODE_API_PORT", "9090"))

class UnixConn(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCK)

def restart_container(name: str) -> bool:
    try:
        enc = urllib.parse.quote(name, safe="")
        conn = UnixConn("localhost", timeout=25)
        conn.request("POST", f"/containers/{{enc}}/restart?t=5", headers={{"Content-Length": "0", "Content-Type": "application/json"}})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status in (204, 200)
    except Exception as e:
        logger.warning("Docker socket restart error: %s", e)
        return False

class NodeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{{"status":"ok"}}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/config":
            self.send_response(404)
            self.end_headers()
            return

        auth = self.headers.get("Authorization", "")
        if not TOKEN or auth != f"Bearer {{TOKEN}}":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{{"ok":false,"error":"Invalid token"}}')
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception as e:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({{"ok": False, "error": f"Invalid JSON: {{e}}"}}).encode())
            return

        new_raw = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cur_raw = json.dumps(json.loads(f.read()), indent=2, ensure_ascii=False, sort_keys=True)
                if cur_raw == new_raw:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{{"ok":true,"restarted":false,"unchanged":true}}')
                    return
        except Exception:
            pass

        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                f.write(new_raw)
            restarted = restart_container(CONTAINER)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({{"ok": True, "restarted": restarted}}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({{"ok": False, "error": str(e)}}).encode())

MASTER_POLL_URL = os.getenv("MASTER_POLL_URL", "")

def poll_master_worker():
    if not MASTER_POLL_URL or not TOKEN:
        return
    import time
    import urllib.request
    logger.info("Background config poll worker started -> %s", MASTER_POLL_URL)
    last_hash = ""
    while True:
        try:
            url = f"{{MASTER_POLL_URL.rstrip('/')}}/api/nodes/poll/{{TOKEN}}"
            req = urllib.request.Request(url, headers={{"User-Agent": "UFOBZK-Node-Agent/1.0"}})
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    new_hash = data.get("hash", "")
                    new_cfg = data.get("config")
                    if new_cfg and new_hash != last_hash:
                        raw = json.dumps(new_cfg, indent=2, ensure_ascii=False, sort_keys=True)
                        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                            f.write(raw)
                        restarted = restart_container(CONTAINER)
                        last_hash = new_hash
                        logger.info("Config pulled from master and applied! (hash: %s, restarted: %s)", new_hash, restarted)
        except Exception as e:
            logger.debug("Poll error: %s", e)
        time.sleep(30)

if __name__ == "__main__":
    import threading
    t = threading.Thread(target=poll_master_worker, daemon=True)
    t.start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), NodeHandler)
    logger.info("Node Agent listening on port %s...", PORT)
    server.serve_forever()
AGENT_EOF

# 8. Seed Sing-box config
cat << SEED_EOF > "$WORK_DIR/config/config.json"
{{
  "log": {{"level": "warn", "timestamp": true}},
  "inbounds": [
    {{
      "type": "vless",
      "tag": "vless-reality-in",
      "listen": "0.0.0.0",
      "listen_port": $REALITY_PORT,
      "users": [],
      "tls": {{
        "enabled": true,
        "server_name": "$REALITY_SNI",
        "reality": {{
          "enabled": true,
          "handshake": {{"server": "$REALITY_SNI", "server_port": 443}},
          "private_key": "$PRIVATE_KEY",
          "short_id": ["$SHORT_ID"]
        }}
      }}
    }},
    {{
      "type": "hysteria2",
      "tag": "hy2-in",
      "listen": "0.0.0.0",
      "listen_port": $HYSTERIA_PORT,
      "users": [],
      "tls": {{
        "enabled": true,
        "server_name": "$QUIC_SNI",
        "certificate_path": "/etc/sing-box/cert.crt",
        "key_path": "/etc/sing-box/cert.key"
      }}
    }},
    {{
      "type": "tuic",
      "tag": "tuic-in",
      "listen": "0.0.0.0",
      "listen_port": $TUIC_PORT,
      "users": [],
      "congestion_control": "bbr",
      "tls": {{
        "enabled": true,
        "server_name": "$QUIC_SNI",
        "certificate_path": "/etc/sing-box/cert.crt",
        "key_path": "/etc/sing-box/cert.key"
      }}
    }}
  ],
  "outbounds": [
    {{"type": "direct", "tag": "direct"}},
    {{"type": "block", "tag": "block"}}
  ],
  "route": {{
    "rules": [
      {{"protocol": "bittorrent", "outbound": "block"}},
      {{"ip_is_private": true, "outbound": "block"}}
    ],
    "auto_detect_interface": true
  }}
}}
SEED_EOF
ok "Seed configuration written"

# 9. Docker Compose
cat << COMPOSE_EOF > "$WORK_DIR/docker-compose.yml"
services:
  sing-box:
    image: ghcr.io/sagernet/sing-box:latest
    container_name: ufobzk-node-core
    restart: unless-stopped
    network_mode: host
    logging:
      driver: "json-file"
      options: {{ max-size: "10m", max-file: "3" }}
    volumes:
      - ./config:/etc/sing-box
    entrypoint:
      - /bin/sh
      - -c
      - "until [ -f /etc/sing-box/config.json ]; do sleep 1; done; exec sing-box run -c /etc/sing-box/config.json"

  node-api:
    image: python:3.11-alpine
    container_name: ufobzk-node-api
    restart: unless-stopped
    network_mode: host
    logging:
      driver: "json-file"
      options: {{ max-size: "10m", max-file: "3" }}
    environment:
      - XRAY_NODE_TOKEN=$API_TOKEN
      - XRAY_CONFIG_PATH=/etc/sing-box/config.json
      - XRAY_CONTAINER_NAME=ufobzk-node-core
      - NODE_API_PORT=$API_PORT
      - MASTER_POLL_URL=$MASTER_URL
    volumes:
      - ./config:/etc/sing-box
      - /var/run/docker.sock:/var/run/docker.sock
      - ./api:/app
    working_dir: /app
    entrypoint:
      - python
      - -u
      - /app/main.py
    depends_on:
      - sing-box
COMPOSE_EOF

# 10. Start containers
log "Starting Sing-box & Node Agent..."
$DC down --remove-orphans 2>/dev/null || true
$DC up -d
ok "Containers launched"

# Ожидание готовности Node Agent API
log "Waiting for Node Agent API to become ready..."
API_READY=0
for i in $(seq 1 20); do
    if curl -fsSL --max-time 2 "http://127.0.0.1:$API_PORT/health" &>/dev/null; then
        API_READY=1
        ok "Node Agent API is up and listening on port $API_PORT"
        break
    fi
    sleep 1
done
if [ "$API_READY" -eq 0 ]; then
    warn "Node Agent API not answering on /health yet. Checking container logs:"
    $DC logs --tail 20 node-api || true
fi

# 11. Firewall — disable UFW to avoid QUIC/UDP packet drops and API timeouts
if command -v ufw &>/dev/null; then
    ufw disable >/dev/null 2>&1 || true
    ok "UFW disabled (ensures direct low-latency QUIC and TCP routing)"
fi
iptables -P INPUT ACCEPT 2>/dev/null || true
iptables -P FORWARD ACCEPT 2>/dev/null || true
iptables -I INPUT -p tcp --dport "$REALITY_PORT" -j ACCEPT 2>/dev/null || true
iptables -I INPUT -p udp --dport "$HYSTERIA_PORT" -j ACCEPT 2>/dev/null || true
iptables -I INPUT -p udp --dport "$TUIC_PORT" -j ACCEPT 2>/dev/null || true
iptables -I INPUT -p tcp --dport "$API_PORT" -j ACCEPT 2>/dev/null || true

# 12. Register back with Master Control Plane
log "Registering node with Master Server ($MASTER_URL)..."
sleep 2
REG_PAYLOAD=$(cat << REG_EOF
{{
  "install_token": "$INSTALL_TOKEN",
  "host": "$NODE_IP",
  "reality_public_key": "$PUBLIC_KEY",
  "reality_private_key": "$PRIVATE_KEY",
  "reality_short_id": "$SHORT_ID",
  "api_port": $API_PORT
}}
REG_EOF
)

REG_RESP=$(curl -fsSL -X POST "$MASTER_URL/api/nodes/register" \\
  -H "Content-Type: application/json" \\
  -d "$REG_PAYLOAD" 2>/dev/null || echo "")

if echo "$REG_RESP" | grep -q '"ok":true'; then
    ok "Node successfully registered with Master Server!"
    echo "$REG_RESP" > "$WORK_DIR/reg_resp.json"
    APPLY_RES=$(python3 -c "
import json
try:
    with open('$WORK_DIR/reg_resp.json', 'r', encoding='utf-8') as f:
        d = json.load(f)
    cfg = d.get('config')
    if cfg:
        with open('$WORK_DIR/config/config.json', 'w', encoding='utf-8') as out:
            json.dump(cfg, out, indent=2, ensure_ascii=False)
        print('OK')
except Exception:
    pass
" 2>/dev/null || true)
    rm -f "$WORK_DIR/reg_resp.json"
    if [ "$APPLY_RES" = "OK" ]; then
        ok "Full configuration received and applied from master!"
        $DC restart sing-box >/dev/null 2>&1 || true
    fi
else
    warn "Registration response: $REG_RESP"
fi

echo ""
echo -e "${{GREEN}}====================================================${{NC}}"
echo -e "${{GREEN}}${{BOLD}}  UFOBZK VPN Node ready!${{NC}}"
echo -e "  Node Name:  ${{BOLD}}${{NODE_NAME}}${{NC}}"
echo -e "  Host IP:    ${{BOLD}}${{NODE_IP}}${{NC}}"
echo -e "  Role:       ${{BOLD}}${{ROLE}}${{NC}}"
echo -e "  Protocols:  VLESS-REALITY (:${{REALITY_PORT}} TCP)"
echo -e "              Hysteria 2 (:${{HYSTERIA_PORT}} UDP/QUIC)"
echo -e "              TUIC v5 (:${{TUIC_PORT}} UDP/QUIC)"
echo -e "  Control:    cd /opt/ufobzk-node && ${{DC}} logs -f"
echo -e "${{GREEN}}====================================================${{NC}}"
echo ""
"""
    return script
