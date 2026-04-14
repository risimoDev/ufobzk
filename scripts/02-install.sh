#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  02-install.sh — Полная установка проекта на сервер
# ═══════════════════════════════════════════════════════════
#  Что делает:
#   • Клонирует репозиторий (или обновляет)
#   • Создаёт .env из шаблона с интерактивным заполнением
#   • Генерирует SECRET_KEY
#   • Получает SSL-сертификат (Let's Encrypt)
#   • Собирает образы и запускает стек
#   • Проверяет здоровье всех сервисов
#
#  Запуск: sudo bash scripts/02-install.sh
#  Предварительно: 01-prepare-server.sh
# ═══════════════════════════════════════════════════════════

set -euo pipefail

# ── Цвета ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

log()  { echo -e "${CYAN}[•]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

# ── Проверки ──

if [ "$(id -u)" -ne 0 ]; then
    err "Запустите от root: sudo bash $0"
    exit 1
fi

if ! command -v docker &>/dev/null; then
    err "Docker не установлен. Сначала выполните: bash scripts/01-prepare-server.sh"
    exit 1
fi

echo ""
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo -e "${CYAN}  Установка VPNBZK — каскадный VPN         ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo ""

# ══════════════════════════════════════════
# 1. Директория проекта
# ══════════════════════════════════════════

PROJECT_DIR="/opt/vpnbzk"

# Вычисляем путь к репо ДО смены директории (иначе относительный путь сломается)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

if [ -d "$PROJECT_DIR" ]; then
    ok "Директория $PROJECT_DIR уже существует"
    cd "$PROJECT_DIR"
else
    log "Создание директории проекта..."
    mkdir -p "$PROJECT_DIR"
    cd "$PROJECT_DIR"
    ok "Директория создана: $PROJECT_DIR"
fi

# Если вызвали из клонированного репо — копируем
if [ -f "$REPO_DIR/docker-compose.yml" ] && [ "$REPO_DIR" != "$PROJECT_DIR" ]; then
    log "Копирование файлов проекта..."
    if command -v rsync &>/dev/null; then
        rsync -a --exclude='.git' --exclude='data/' --exclude='.env' \
            "$REPO_DIR/" "$PROJECT_DIR/"
    else
        # Fallback: cp без rsync
        find "$REPO_DIR" -mindepth 1 -maxdepth 1 \
            ! -name '.git' ! -name 'data' ! -name '.env' \
            -exec cp -r {} "$PROJECT_DIR/" \;
    fi
    ok "Файлы скопированы"
fi

cd "$PROJECT_DIR"

# Проверяем наличие ключевых файлов
if [ ! -f "docker-compose.yml" ]; then
    err "docker-compose.yml не найден в $PROJECT_DIR"
    err "Скопируйте файлы проекта вручную или клонируйте репозиторий"
    exit 1
fi

# ══════════════════════════════════════════
# 2. Настройка .env
# ══════════════════════════════════════════

if [ -f ".env" ]; then
    warn ".env уже существует"
    read -rp "$(echo -e "${YELLOW}Перезаписать .env? [y/N]: ${NC}")" OVERWRITE
    if [[ "${OVERWRITE,,}" != "y" ]]; then
        ok "Используем существующий .env"
    else
        WRITE_ENV=true
    fi
else
    WRITE_ENV=true
fi

if [ "${WRITE_ENV:-false}" = true ]; then
    log "Настройка окружения..."
    echo ""

    # Домен
    read -rp "$(echo -e "${BOLD}Домен (например, niiaya.example.com):${NC} ")" INPUT_DOMAIN
    DOMAIN="${INPUT_DOMAIN:?Домен обязателен}"

    # Email для SSL
    read -rp "$(echo -e "${BOLD}Email для Let's Encrypt:${NC} ")" INPUT_EMAIL
    EMAIL="${INPUT_EMAIL:?Email обязателен}"

    # Telegram Bot
    read -rp "$(echo -e "${BOLD}Telegram Bot Token:${NC} ")" INPUT_BOT_TOKEN
    BOT_TOKEN="${INPUT_BOT_TOKEN:?Bot Token обязателен}"

    read -rp "$(echo -e "${BOLD}Telegram Bot Username (без @):${NC} ")" INPUT_BOT_USER
    BOT_USER="${INPUT_BOT_USER:?Bot Username обязателен}"

    # NL-сервер (Нидерланды)
    read -rp "$(echo -e "${BOLD}NL-сервер IP (Нидерланды):${NC} ")" INPUT_NL_IP
    NL_IP="${INPUT_NL_IP:-}"

    # RU-сервер (Россия)
    read -rp "$(echo -e "${BOLD}RU-сервер IP (Россия, необязательно):${NC} ")" INPUT_RU_IP
    RU_IP="${INPUT_RU_IP:-}"

    # Мой IP для whitelist
    MY_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || echo "")
    if [ -n "$MY_IP" ]; then
        read -rp "$(echo -e "${BOLD}Ваш IP для whitelist [${MY_IP}]:${NC} ")" INPUT_IP
        WHITELIST_IP="${INPUT_IP:-$MY_IP}"
    else
        read -rp "$(echo -e "${BOLD}Ваш IP для admin whitelist:${NC} ")" WHITELIST_IP
    fi

    # Генерация SECRET_KEY
    SECRET_KEY=$(openssl rand -hex 32)

    # Запись .env
    cat > .env <<ENVEOF
# ═══════════════════════════════════════════
#  Автоматически сгенерировано install.sh
#  $(date '+%Y-%m-%d %H:%M:%S')
# ═══════════════════════════════════════════

# ── Домен и SSL ──
DOMAIN=${DOMAIN}
EMAIL=${EMAIL}

# ── Секреты приложения ──
SECRET_KEY=${SECRET_KEY}

# ── Telegram Bot ──
TELEGRAM_BOT_TOKEN=${BOT_TOKEN}
TELEGRAM_BOT_USERNAME=${BOT_USER}
WEBAPP_URL=https://${DOMAIN}

# ── Каскадные серверы ──
NL_SERVER_IP=${NL_IP}
RU_SERVER_IP=${RU_IP}

# ── Xray ──
XRAY_CONFIG_PATH=/etc/xray/config.json
VLESS_WS_PORT=443
REALITY_PORT=2053

# ── REALITY (заполняется скриптом 05-setup-reality.sh) ──
REALITY_PUBLIC_KEY=
REALITY_PRIVATE_KEY=
REALITY_SHORT_ID=
REALITY_DEST=www.samsung.com:443
REALITY_SERVER_NAMES=www.samsung.com,samsung.com

# ── Доступ ──
ADMIN_IPS=${WHITELIST_IP}
ENVEOF

    chmod 600 .env
    ok ".env создан (chmod 600)"
fi

# Загружаем env  
source .env

# ══════════════════════════════════════════
# 3. Создание директорий
# ══════════════════════════════════════════

log "Создание директорий данных..."
mkdir -p data
ok "Директории готовы"

# ══════════════════════════════════════════
# 4. SSL-сертификат
# ══════════════════════════════════════════

CERT_PATH="/etc/letsencrypt/live/${DOMAIN}/fullchain.pem"
# Проверяем в Docker-volume или на хосте
CERT_EXISTS=false

if docker volume ls | grep -q 'certbot-certs' 2>/dev/null; then
    # Проверяем внутри volume
    if docker run --rm -v vpnbzk_certbot-certs:/certs:ro alpine \
        test -f "/certs/live/${DOMAIN}/fullchain.pem" 2>/dev/null; then
        CERT_EXISTS=true
    fi
fi

if [ "$CERT_EXISTS" = false ]; then
    log "Получение SSL-сертификата для ${DOMAIN}..."
    echo ""
    warn "Убедитесь, что DNS A-запись ${DOMAIN} → IP этого сервера уже настроена!"
    read -rp "$(echo -e "${YELLOW}DNS настроена? Продолжить? [y/N]: ${NC}")" DNS_READY
    if [[ "${DNS_READY,,}" != "y" ]]; then
        warn "Пропускаем SSL. Запустите вручную позже:"
        warn "  bash init-ssl.sh"
        SKIP_SSL=true
    else
        # Запуск временного nginx для ACME challenge
        log "Запуск временного nginx (HTTP-only)..."

        docker run -d --rm --name vpnbzk-nginx-init \
            -v "$(pwd)/nginx/nginx-initial.conf:/etc/nginx/nginx.conf.template:ro" \
            -v "vpnbzk_certbot-webroot:/var/www/certbot" \
            -p 80:80 \
            -e DOMAIN="${DOMAIN}" \
            --entrypoint "/bin/sh -c 'envsubst \"\\\$$DOMAIN\" < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf && nginx -g \"daemon off;\"'" \
            nginx:1.27-alpine

        sleep 3

        log "Запрос сертификата у Let's Encrypt..."
        docker run --rm \
            -v "vpnbzk_certbot-webroot:/var/www/certbot" \
            -v "vpnbzk_certbot-certs:/etc/letsencrypt" \
            certbot/certbot:latest certonly \
            --webroot -w /var/www/certbot \
            -d "$DOMAIN" \
            --email "$EMAIL" \
            --agree-tos \
            --no-eff-email

        docker stop vpnbzk-nginx-init 2>/dev/null || true
        ok "SSL-сертификат получен"
    fi
else
    ok "SSL-сертификат уже существует"
fi

# ══════════════════════════════════════════
# 5. Сборка и запуск
# ══════════════════════════════════════════

if [ "${SKIP_SSL:-false}" = true ]; then
    warn "SSL не настроен — запускаем без nginx (dev-режим)"
    log "Сборка образов..."
    docker compose -f docker-compose.dev.yml build --quiet
    log "Запуск стека (dev)..."
    docker compose -f docker-compose.dev.yml up -d
else
    log "Сборка образов..."
    docker compose build --quiet
    log "Запуск полного стека..."
    docker compose up -d
fi

# ══════════════════════════════════════════
# 6. Ожидаем здоровья сервисов
# ══════════════════════════════════════════

log "Ожидание запуска сервисов..."

MAX_WAIT=90
ELAPSED=0

while [ $ELAPSED -lt $MAX_WAIT ]; do
    HEALTHY=$(docker compose ps --format json 2>/dev/null | grep -c '"healthy"' || echo "0")
    TOTAL=$(docker compose ps --format json 2>/dev/null | wc -l || echo "0")

    if [ "$HEALTHY" -ge 2 ]; then
        break
    fi

    sleep 5
    ELAPSED=$((ELAPSED + 5))
    echo -ne "\r  ⏳ ${ELAPSED}s... (healthy: ${HEALTHY}/${TOTAL})"
done
echo ""

# Показать статус
docker compose ps

echo ""

# ══════════════════════════════════════════
# 7. Настройка cron для обновления SSL
# ══════════════════════════════════════════

if [ "${SKIP_SSL:-false}" != true ]; then
    CRON_CMD="0 3 * * 0 cd $PROJECT_DIR && docker compose run --rm certbot renew --quiet && docker compose exec nginx nginx -s reload"
    if ! crontab -l 2>/dev/null | grep -q "certbot renew"; then
        (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
        ok "Cron: обновление SSL каждое воскресенье в 03:00"
    else
        ok "Cron для SSL уже настроен"
    fi
fi

# ══════════════════════════════════════════
# 8. Настройка logrotate
# ══════════════════════════════════════════

cat > /etc/logrotate.d/vpnbzk <<'EOF'
/var/lib/docker/containers/*/*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    copytruncate
    maxsize 50M
}
EOF
ok "Logrotate настроен"

# ══════════════════════════════════════════
# 9. Итог
# ══════════════════════════════════════════

echo ""
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ Проект установлен!                    ${NC}"
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo ""
echo -e "  Домен:     ${BOLD}${DOMAIN}${NC}"
echo -e "  Проект:    ${PROJECT_DIR}"
if [ "${SKIP_SSL:-false}" = true ]; then
    echo -e "  Сайт:      http://<IP>:8000"
    echo -e "  Режим:     ${YELLOW}DEV (без SSL)${NC}"
else
    echo -e "  Сайт:      https://${DOMAIN}"
    echo -e "  Админка:   https://${DOMAIN}/admin"
    echo -e "  Режим:     ${GREEN}PRODUCTION${NC}"
fi
echo ""
echo -e "  ${YELLOW}Следующий шаг: bash scripts/05-setup-reality.sh${NC}"
echo ""
echo -e "  ${CYAN}Полезные команды:${NC}"
echo -e "  cd $PROJECT_DIR"
echo -e "  docker compose ps            # статус"
echo -e "  docker compose logs -f       # логи"
echo -e "  bash scripts/03-deploy.sh    # обновление"
echo ""
