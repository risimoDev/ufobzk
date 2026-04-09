#!/bin/bash
# ═══════════════════════════════════════════
#  Первый запуск: получение SSL-сертификата
# ═══════════════════════════════════════════
#
# Запустите этот скрипт ОДИН РАЗ при первом деплое:
#   chmod +x init-ssl.sh && ./init-ssl.sh
#
# После этого используйте обычный: docker compose up -d

set -euo pipefail

# Загрузить переменные
if [ ! -f .env ]; then
    echo "❌ Файл .env не найден. Скопируйте .env.example → .env и заполните."
    exit 1
fi
source .env

if [ -z "${DOMAIN:-}" ] || [ -z "${EMAIL:-}" ]; then
    echo "❌ Установите DOMAIN и EMAIL в .env"
    exit 1
fi

echo "═══ Шаг 1: Запуск nginx с HTTP-only конфигом ═══"

# Подменяем конфиг на начальный (без SSL)
docker compose run -d --rm --name vpnbzk-nginx-init \
    -v "$(pwd)/nginx/nginx-initial.conf:/etc/nginx/nginx.conf.template:ro" \
    -v "vpnbzk_certbot-webroot:/var/www/certbot" \
    -p 80:80 \
    --entrypoint "/bin/sh -c 'envsubst \"\\\$$DOMAIN\" < /etc/nginx/nginx.conf.template > /etc/nginx/nginx.conf && nginx -g \"daemon off;\"'" \
    nginx

echo "⏳ Ожидание nginx..."
sleep 3

echo "═══ Шаг 2: Получение сертификата Let's Encrypt ═══"

docker compose run --rm certbot certonly \
    --webroot -w /var/www/certbot \
    -d "$DOMAIN" \
    --email "$EMAIL" \
    --agree-tos \
    --no-eff-email

echo "═══ Шаг 3: Остановка временного nginx ═══"
docker stop vpnbzk-nginx-init 2>/dev/null || true

echo ""
echo "✅ Сертификат получен!"
echo "   Теперь запускайте полный стек:"
echo "   docker compose up -d"
echo ""
