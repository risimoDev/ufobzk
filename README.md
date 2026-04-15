# 🛸 VPNBZK — Каскадный VPN на Xray

Веб-приложение для управления каскадным VPN на голом ядре Xray с космической тематикой.

## Архитектура

```
Клиент → RU-сервер (Россия)
              ├── .ru / .su / .рф / geoip:ru → DIRECT (напрямую)
              └── всё остальное → NL-сервер (Нидерланды) → Интернет
```

| Компонент   | Технология                      |
| ----------- | ------------------------------- |
| Backend     | Python FastAPI                  |
| Frontend    | Jinja2 + TailwindCSS (CDN)      |
| Авторизация | Telegram-бот (одноразовые коды) |
| VPN-ядро    | Xray (VLESS WS+TLS, REALITY)    |
| База данных | SQLite через SQLAlchemy         |
| Деплой      | Docker Compose                  |

## Серверы

| Сервер | Расположение | Роль                                                              |
| ------ | ------------ | ----------------------------------------------------------------- |
| **NL** | Нидерланды   | Основной сервер + веб-панель. Весь нерусский трафик выходит здесь |
| **RU** | Россия       | Точка входа. .ru/.su/.рф → напрямую, остальное → каскад в NL      |

## Протоколы подключения

| Протокол      | Порт | Описание                                  |
| ------------- | ---- | ----------------------------------------- |
| VLESS WS+TLS  | 443  | Через CDN (Cloudflare). Лучшая маскировка |
| VLESS REALITY | 2053 | Прямое подключение. Быстрее, но без CDN   |

## Структура проекта

```
vpnbzk/
├── app/
│   ├── main.py          # FastAPI — маршруты, lifespan, middleware
│   ├── auth.py          # Генерация и проверка одноразовых кодов
│   ├── xray.py          # Управление Xray — конфиги, ссылки, перезагрузка
│   ├── models.py        # SQLAlchemy модели (User, VPNKey, AuthCode)
│   ├── bot.py           # Telegram-бот (aiogram 3)
│   ├── templates/       # Jinja2 шаблоны (космическая тема)
│   └── static/
├── xray/
│   ├── xray-nl.json     # Шаблон конфига для NL-сервера
│   └── xray-ru.json     # Шаблон конфига для RU-сервера (каскад)
├── scripts/
│   ├── 01-prepare-server.sh   # Подготовка ОС (Docker, UFW, swap)
│   ├── 02-install.sh          # Полная установка на основной сервер
│   ├── 03-deploy.sh           # Обновление (zero-downtime)
│   ├── 05-setup-reality.sh    # Генерация REALITY-ключей
│   ├── setup-nl-server.sh     # Установка Xray на NL-сервер
│   └── setup-ru-server.sh     # Установка Xray на RU-сервер
├── nginx/
│   └── nginx.conf       # Reverse proxy + SSL termination
├── docker-compose.yml   # Production (Nginx + Xray + App + Certbot)
├── docker-compose.dev.yml # Разработка (App + Xray)
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## Установка с нуля — пошаговая инструкция

### Шаг 1. Подготовка серверов

Вам нужны 2 VPS:

- **NL-сервер** (Нидерланды или любая Европа) — основной, здесь будет веб-панель
- **RU-сервер** (Россия) — точка входа для пользователей

Требования: Ubuntu 22.04+, минимум 1 ГБ RAM, 1 CPU.

### Шаг 2. Подготовка NL-сервера (основной)

```bash
# SSH на NL-сервер
ssh root@<NL_IP>

# Подготовка системы
bash scripts/01-prepare-server.sh

# Установка Xray + генерация транзитного UUID
bash scripts/setup-nl-server.sh
```

**Сохраните вывод скрипта!** Вам понадобятся:

- `REALITY Public Key`
- `REALITY Private Key`
- `Short ID`
- `Transit UUID`
- `NL_SERVER_IP`

### Шаг 3. Установка основного стека на NL-сервер

```bash
# Установка проекта (Docker Compose)
bash scripts/02-install.sh
```

Скрипт спросит:

- Домен (например, `vpn.example.com`)
- Email для SSL
- Telegram Bot Token и Username
- IP серверов (NL и RU)
- Ваш IP для whitelist админки

### Шаг 4. Настройка REALITY на основном сервере

```bash
bash scripts/05-setup-reality.sh
```

Запишите `Public Key` и `Short ID` — они нужны клиентам.

### Шаг 5. Настройка RU-сервера (каскад)

```bash
# SSH на RU-сервер
ssh root@<RU_IP>

# Подготовка системы
bash scripts/01-prepare-server.sh

# Установка Xray-каскада
bash scripts/setup-ru-server.sh
```

Скрипт спросит данные от NL-сервера (из шага 2):

- NL-сервер IP
- NL REALITY Public Key
- NL REALITY Short ID
- Transit UUID

### Шаг 6. DNS

Настройте A-запись:

```
vpn.example.com → <NL_SERVER_IP>
```

Если используете Cloudflare CDN — включите проксирование (оранжевое облако).

### Шаг 7. Первый администратор

1. Напишите вашему Telegram-боту `/start`
2. Получите код и войдите на сайт
3. Назначьте себя суперадмином:

```bash
docker compose exec ufo-app python -c "
from app.models import SessionLocal, User
db = SessionLocal()
u = db.query(User).first()
u.is_admin = True
db.commit()
print(f'Пользователь {u.telegram_id} назначен администратором.')
"
```

Или задайте `SUPERADMIN_TELEGRAM_ID` в `.env`.

---

## Управление

### Админ-панель

Доступна по `https://ваш-домен/admin` (только с разрешённых IP).

Возможности:

- Просмотр всех пользователей
- Создание/удаление VPN-ключей (несколько на пользователя)
- Включение/отключение ключей
- Установка лимитов трафика и срока действия
- Синхронизация конфига Xray

### Личный кабинет пользователя

Доступен по `https://ваш-домен/cabinet`.

Показывает:

- Все VPN-ключи пользователя
- Ссылки для подключения (с кнопкой «Скопировать»)
- URL подписки для автообновления
- Инструкции для iOS, Android, Windows, macOS

### Подписки

URL подписки для клиентов: `https://ваш-домен/sub/<user_id>`

Поддерживается автообновление в:

- v2rayNG (Android)
- Hiddify (Android/iOS)
- Streisand (iOS)
- V2Box (iOS)
- Nekoray (Windows/macOS/Linux)

---

## Полезные команды

```bash
# Статус всех контейнеров
docker compose ps

# Логи в реальном времени
docker compose logs -f

# Логи конкретного сервиса
docker compose logs -f ufo-app
docker compose logs -f xray

# Перезапуск после изменения .env
docker compose down && docker compose up -d

# Обновление проекта
bash scripts/03-deploy.sh

# Бэкап БД
cp data/vpnbzk.db data/vpnbzk.db.bak
```

## Переменные окружения (.env)

| Переменная              | Описание                  |
| ----------------------- | ------------------------- |
| `DOMAIN`                | Домен сайта               |
| `SECRET_KEY`            | Секрет для подписи сессий |
| `TELEGRAM_BOT_TOKEN`    | Токен Telegram-бота       |
| `TELEGRAM_BOT_USERNAME` | Username бота (без @)     |
| `NL_SERVER_IP`          | IP NL-сервера             |
| `RU_SERVER_IP`          | IP RU-сервера             |
| `REALITY_PUBLIC_KEY`    | Публичный ключ REALITY    |
| `REALITY_PRIVATE_KEY`   | Приватный ключ REALITY    |
| `REALITY_SHORT_ID`      | Short ID для REALITY      |
| `ADMIN_IPS`             | IP для доступа к админке  |


