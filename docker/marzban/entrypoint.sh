#!/bin/bash
set -e

CERT_DIR="/var/lib/marzban/certs"
CERT_FILE="${CERT_DIR}/internal.crt"
KEY_FILE="${CERT_DIR}/internal.key"

# ── Генерация самоподписанного сертификата (один раз) ──
# Нужен чтобы Marzban слушал на 0.0.0.0, а не 127.0.0.1
# SSL-терминация для клиентов идёт через nginx
mkdir -p "$CERT_DIR"

if [ ! -f "$CERT_FILE" ]; then
    echo "→ Генерация self-signed сертификата для внутреннего HTTPS..."
    openssl req -x509 -newkey rsa:2048 \
        -keyout "$KEY_FILE" \
        -out "$CERT_FILE" \
        -days 3650 -nodes \
        -subj "/CN=marzban" 2>/dev/null
    echo "[✓] Сертификат создан: ${CERT_FILE}"
fi

# ── Миграции БД ──
echo "→ Применение миграций..."
alembic upgrade head

# ── Запуск Marzban ──
echo "→ Запуск Marzban..."
exec python main.py
