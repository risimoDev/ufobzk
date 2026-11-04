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
import logging
import os
import socket
import urllib.parse
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("node-agent")

TOKEN = os.getenv("XRAY_NODE_TOKEN", "")
CONFIG_PATH = os.getenv("XRAY_CONFIG_PATH", "/etc/sing-box/config.json")
CONTAINER = os.getenv("XRAY_CONTAINER_NAME", "ufobzk-node-core")
DOCKER_SOCK = "/var/run/docker.sock"

app = FastAPI(title="Node Agent")

class UnixConn(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCK)

def restart_container(name: str) -> bool:
    try:
        enc = urllib.parse.quote(name, safe="")
        conn = UnixConn("localhost", timeout=20)
        conn.request("POST", f"/containers/{{enc}}/restart?t=5", headers={{"Content-Length": "0", "Content-Type": "application/json"}})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        return resp.status == 204
    except Exception as e:
        logger.warning("Docker socket restart error: %s", e)
        return False

@app.get("/health")
async def health():
    return {{"status": "ok"}}

@app.post("/config")
async def apply_config(request: Request, authorization: str | None = Header(None)):
    if not TOKEN or authorization != f"Bearer {{TOKEN}}":
        raise HTTPException(status_code=401, detail="Invalid token")
    import json
    data = await request.json()
    new_raw = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cur_raw = json.dumps(json.loads(f.read()), indent=2, ensure_ascii=False, sort_keys=True)
            if cur_raw == new_raw:
                return JSONResponse({{"ok": True, "restarted": False, "unchanged": True}})
    except Exception:
        pass

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(new_raw)

    restarted = restart_container(CONTAINER)
    return JSONResponse({{"ok": True, "restarted": restarted}})
AGENT_EOF

# 8. Seed Sing-box config
cat << SEED_EOF > "$WORK_DIR/config/config.json"
{{
  "log": {{"level": "warn", "timestamp": true}},
  "inbounds": [
    {{
      "type": "vless",
      "tag": "vless-reality-in",
      "listen": "::",
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
      "listen": "::",
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
      "listen": "::",
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
    volumes:
      - ./config:/etc/sing-box
      - /var/run/docker.sock:/var/run/docker.sock
      - ./api:/app
    working_dir: /app
    entrypoint:
      - /bin/sh
      - -c
      - "pip install --no-cache-dir fastapi uvicorn && python -m uvicorn main:app --host 0.0.0.0 --port $API_PORT"
    depends_on:
      - sing-box
COMPOSE_EOF

# 10. Start containers
log "Starting Sing-box & Node Agent..."
$DC down --remove-orphans 2>/dev/null || true
$DC up -d
ok "Containers launched"

# 11. Firewall
if command -v ufw &>/dev/null; then
    ufw allow 22/tcp comment 'SSH' >/dev/null 2>&1 || true
    ufw allow $REALITY_PORT/tcp comment 'REALITY' >/dev/null 2>&1 || true
    ufw allow $HYSTERIA_PORT/udp comment 'Hysteria2' >/dev/null 2>&1 || true
    ufw allow $TUIC_PORT/udp comment 'TUIC' >/dev/null 2>&1 || true
    ufw allow $API_PORT/tcp comment 'Node-API' >/dev/null 2>&1 || true
    ufw --force enable >/dev/null 2>&1 || true
    ok "Firewall configured (TCP $REALITY_PORT, UDP $HYSTERIA_PORT, UDP $TUIC_PORT, TCP $API_PORT)"
fi

# 12. Register back with Master Control Plane
log "Registering node with Master Server ($MASTER_URL)..."
sleep 4
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
