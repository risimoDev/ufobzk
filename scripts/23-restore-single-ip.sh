#!/usr/bin/env bash
# Новый VPS с одним IPv4. Запускать из копии проекта, содержащей серверный .env.
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")/.."

fail() { printf '%s\n' "$*" >&2; exit 1; }

case "${1:-}" in
    install-docker) [[ $# == 1 ]] || fail "Использование: $0 install-docker" ;;
    prepare) [[ $# == 3 ]] || fail "Использование: $0 prepare <публичный IPv4> <бэкап SQLite>" ;;
    finish)  [[ $# == 1 ]] || fail "Использование: $0 finish" ;;
    *) fail "Использование: $0 install-docker | prepare <публичный IPv4> <бэкап SQLite> | finish" ;;
esac
stage=$1

if [[ $stage == install-docker ]]; then
    [[ $EUID == 0 ]] || fail "Запустите установку Docker через sudo."
    if ! docker compose version >/dev/null 2>&1; then
        . /etc/os-release
        case "$ID" in ubuntu|debian) ;; *) fail "Автоустановка поддерживает Ubuntu и Debian." ;; esac
        apt-get update
        apt-get install -y ca-certificates curl python3
        install -d -m 0755 /etc/apt/keyrings
        curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
        chmod 644 /etc/apt/keyrings/docker.asc
        cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $VERSION_CODENAME
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
        chmod 644 /etc/apt/sources.list.d/docker.sources
        apt-get update
        apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    fi
    systemctl enable --now docker
    docker compose version
    exit 0
fi

[[ -f .env ]] || fail "Положите серверный .env рядом с docker-compose.yml или заполните копию .env.example."
chmod 600 .env
command -v python3 >/dev/null && command -v curl >/dev/null || fail "Установите python3 и curl."
docker compose version >/dev/null || fail "Установите Docker Engine и Docker Compose plugin."
docker info >/dev/null || fail "Запустите Docker и выполните сценарий с доступом к Docker."

# Compose получает адрес и образ из подготовленного .env.
unset MAIN_WEB_IP REALITY_BIND_IP NL_SERVER_IP VLESS_WS_PORT REALITY_PORT DOMAIN EMAIL XRAY_IMAGE

if [[ $stage == prepare ]]; then
    backup=$(realpath -- "$3")
    [[ -f $backup ]] || fail "Файл бэкапа не найден."
    work=$(mktemp -d)
    trap 'rm -rf -- "$work"' EXIT

    # Читаем SQLite через backup API. Восстановление учитывает WAL, если он
    # присутствует рядом с исходной БД. Исходный файл открывается только для чтения.
    python3 - "$backup" "$work/restore.db" <<'PY'
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

source, snapshot = map(Path, sys.argv[1:])
with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as db:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"users", "vpn_keys"} <= tables:
        sys.exit("В бэкапе отсутствуют таблицы users или vpn_keys.")
    with closing(sqlite3.connect(snapshot)) as copy:
        db.backup(copy)
        if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            sys.exit("Бэкап SQLite не прошёл проверку целостности.")
        copy.execute("PRAGMA journal_mode=DELETE")
        for table in ("users", "vpn_keys"):
            count, = copy.execute(f"SELECT count(*) FROM {table}").fetchone()
            print(f"{table}: {count}")
PY

    python3 - "$2" <<'PY'
import ipaddress
import os
from pathlib import Path
import re
import secrets
import sys

ip = ipaddress.IPv4Address(sys.argv[1])
if not ip.is_global:
    sys.exit("Укажите публичный IPv4 нового VPS.")
path = Path(".env")
lines = path.read_text(encoding="utf-8-sig").splitlines()
assign = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
values = {}
for line in lines:
    if match := assign.match(line):
        raw = re.split(r"\s+#", match[2], maxsplit=1)[0].strip()
        values[match[1]] = raw.strip("'\"")

domain, email = (values.get(key, "") for key in ("DOMAIN", "EMAIL"))
if domain in {"", "vpn.example.com"} or email in {"", "admin@example.com"}:
    sys.exit("Заполните реальные DOMAIN и EMAIL в .env.")

updates = {
    "NL_SERVER_IP": str(ip),
    "MAIN_WEB_IP": "",
    "REALITY_BIND_IP": "",
    "VLESS_WS_PORT": "443",
    "REALITY_PORT": "2053",
}
if not values.get("WEBAPP_URL") or values["WEBAPP_URL"] == "https://vpn.example.com":
    updates["WEBAPP_URL"] = f"https://{domain}"
if not values.get("SECRET_KEY") or values["SECRET_KEY"].startswith("change-me"):
    updates["SECRET_KEY"] = secrets.token_hex(32)
keys = ("REALITY_PRIVATE_KEY", "REALITY_PUBLIC_KEY", "REALITY_SHORT_ID")
configured_keys = [key for key in keys if values.get(key)]
if not configured_keys:
    updates.update(dict.fromkeys(keys, ""))
elif len(configured_keys) != len(keys):
    sys.exit("Для REALITY нужны все три значения. Восстановите недостающие или очистите все три для генерации.")
if not values.get("ADMIN_PASSWORD") and not values.get("TELEGRAM_BOT_TOKEN"):
    updates["ADMIN_PASSWORD"] = secrets.token_urlsafe(24)
saved_path = Path("data/.env.before-one-ip")
saved_path.parent.mkdir(exist_ok=True)
if not saved_path.exists():
    with saved_path.open("xb") as saved:
        saved.write(path.read_bytes())

out = [line for line in lines if not (match := assign.match(line)) or match[1] not in updates]
out.extend(f"{key}={value}" for key, value in updates.items())
temporary = path.with_suffix(".one-ip-tmp")
temporary.write_text("\n".join(out) + "\n", encoding="utf-8")
os.chmod(temporary, 0o600)
temporary.replace(path)
PY

    docker compose pull xray
    if ! grep -Eq '^[[:space:]]*(export[[:space:]]+)?REALITY_PRIVATE_KEY[[:space:]]*=[[:space:]]*[^[:space:]#]' .env; then
        image=$(docker compose config --no-env-resolution --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["xray"]["image"])')
        material=$(docker run --rm --entrypoint xray "$image" x25519)
        private_key=$(awk -F ': *' 'tolower($1) ~ /^private ?key$/ {print $2}' <<< "$material")
        public_key=$(awk -F ': *' 'tolower($1) ~ /^(public ?key|password( \(publickey\))?)$/ {print $2}' <<< "$material")
        [[ -n $private_key && -n $public_key ]] || fail "Не удалось сгенерировать ключи REALITY."
        short_id=$(python3 -c 'import secrets; print(secrets.token_hex(8))')
        sed -i -E '/^[[:space:]]*(export[[:space:]]+)?REALITY_(PRIVATE_KEY|PUBLIC_KEY|SHORT_ID)[[:space:]]*=/d' .env
        printf '\nREALITY_PRIVATE_KEY=%s\nREALITY_PUBLIC_KEY=%s\nREALITY_SHORT_ID=%s\n' "$private_key" "$public_key" "$short_id" >> .env
        unset material private_key public_key short_id
        echo "Созданы новые ключи REALITY. Пользователям понадобится обновить подписку."
    fi

    docker compose build ufo-app
    docker compose run --rm --no-deps -T \
        -v "$work/restore.db:/restore.db:ro" --entrypoint python ufo-app -c '
import os
from pathlib import Path
from shutil import copyfile
from tempfile import NamedTemporaryFile

target = Path("/project/data/vpnbzk.db")
marker = target.with_name(".one-ip-restored")
if target.exists():
    if not marker.is_file():
        raise SystemExit("В Docker-томе уже есть база. Сценарий восстановления рассчитан на новый VPS.")
    print("База уже восстановлена ранее.")
else:
    with NamedTemporaryFile(dir=target.parent) as tmp:
        copyfile("/restore.db", tmp.name)
        marker.touch()
        os.link(tmp.name, target)
    print("База восстановлена в том app-data.")
'
    docker compose up -d --wait --wait-timeout 180 xray ufo-app
    docker compose exec -T xray xray run -test -c /etc/xray/config.json
    docker compose exec -T xray xray version
    printf '\nREALITY слушает TCP 2053. Для HTTPS направьте DNS домена на этот VPS, затем запустите %s finish.\n' "$0"
    exit 0
fi

docker compose exec -T ufo-app test -f /project/data/.one-ip-restored ||
    fail "Перед настройкой HTTPS выполните этап prepare."

domain=$(docker compose config --no-env-resolution --format json | python3 -c '
import json, sys
env = json.load(sys.stdin)["services"]["nginx"]["environment"]
print(env["DOMAIN"])
')
email=$(sed -nE "s/^[[:space:]]*(export[[:space:]]+)?EMAIL[[:space:]]*=[[:space:]]*([^[:space:]#]+).*/\2/p" .env | tail -1 | tr -d "\"'")
[[ -n $domain && -n $email ]] || fail "Заполните DOMAIN и EMAIL в .env."

if ! docker compose run --no-deps --rm -T --entrypoint /bin/sh certbot \
    -c 'test -s "/etc/letsencrypt/live/$1/fullchain.pem" && test -s "/etc/letsencrypt/live/$1/privkey.pem"' _ "$domain"; then
    bootstrap="ufobzk-nginx-init-$$"
    docker compose run --no-deps -d --rm --name "$bootstrap" -p 80:80 \
        -v "$PWD/nginx/nginx-initial.conf:/etc/nginx/nginx.conf.template:ro" \
        --entrypoint /bin/sh nginx -ec \
        'envsubst "\$DOMAIN" < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf; exec nginx -g "daemon off;"'
    trap 'docker stop "$bootstrap" >/dev/null 2>&1 || true' EXIT
    docker compose run --no-deps --rm -T --entrypoint certbot certbot certonly \
        --webroot -w /var/www/certbot -d "$domain" --email "$email" \
        --agree-tos --no-eff-email --non-interactive
    docker stop "$bootstrap" >/dev/null
    trap - EXIT
fi

# MTProto запускается, только если конфиг уже сгенерирован в постоянном томе.
services=(nginx certbot)
if docker compose exec -T ufo-app test -s /etc/mtg/config.toml; then
    services+=(mtg)
fi
docker compose up -d --wait --wait-timeout 180 "${services[@]}"
curl -fsS --max-time 15 --noproxy '*' --resolve "$domain:443:127.0.0.1" "https://$domain/" -o /dev/null
PROJECT_DIR="$PWD" bash scripts/18-client-test.sh
