# 🛸 VPNBZK — Система управления VPN

Веб-приложение для управления VPN-подключениями с космической тематикой.

## Архитектура

| Компонент   | Технология                      |
| ----------- | ------------------------------- |
| Backend     | Python FastAPI                  |
| Frontend    | Jinja2 + TailwindCSS (CDN)      |
| Авторизация | Telegram-бот (одноразовые коды) |
| VPN-панель  | Marzban (REST API)              |
| База данных | SQLite через SQLAlchemy         |
| Деплой      | Docker Compose                  |

## Структура проекта

```
vpnbzk/
├── app/
│   ├── __init__.py
│   ├── main.py          # FastAPI — маршруты, lifespan, middleware
│   ├── auth.py          # Генерация и проверка одноразовых кодов
│   ├── marzban.py       # Обёртка Marzban REST API
│   ├── models.py        # SQLAlchemy модели (User, AuthCode)
│   ├── bot.py           # Telegram-бот (aiogram 3)
│   ├── templates/
│   │   ├── index.html   # Главная — космическая тема
│   │   ├── login.html   # Ввод кода
│   │   ├── cabinet.html # Личный кабинет (конфиг, QR, трафик)
│   │   └── admin.html   # Админ-панель
│   └── static/
│       └── style.css
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── .env.example
├── .env.marzban.example
└── .gitignore
```

## Быстрый старт

### 1. Конфигурация

```bash
cp .env.example .env
cp .env.marzban.example .env.marzban
```

Отредактируйте `.env`:

- `SECRET_KEY` — случайная строка
- `TELEGRAM_BOT_TOKEN` — токен от @BotFather
- `WEBAPP_URL` — публичный URL вашего сайта
- `MARZBAN_ADMIN_USER` / `MARZBAN_ADMIN_PASS` — данные администратора Marzban

### 2. Запуск

```bash
docker compose up -d --build
```

Приложение будет доступно на `http://localhost:8000`.  
Marzban-панель — на `http://localhost:8880`.

### 3. Создание первого администратора

После первого входа через бота выполните в БД:

```bash
docker compose exec web python -c "
from app.models import SessionLocal, User
db = SessionLocal()
u = db.query(User).first()
u.is_admin = True
db.commit()
print(f'Пользователь {u.telegram_id} назначен администратором.')
"
```

## Как работает авторизация

1. Пользователь пишет боту `/login` в Telegram
2. Бот создаёт запись в `auth_codes` с 6-значным кодом (TTL 5 минут)
3. Пользователь вводит код на `/login`
4. Система проверяет код, создаёт/обновляет пользователя, выдаёт cookie-сессию

## Компоненты

### `models.py`

- **User** — пользователь (telegram_id, marzban_username, is_admin, is_active)
- **AuthCode** — одноразовые коды входа с TTL

### `auth.py`

- `generate_auth_code()` — создаёт 6-значный код
- `verify_auth_code()` — проверяет код, возвращает пользователя

### `marzban.py`

- Асинхронный HTTP-клиент для Marzban API
- Автоматическое получение и обновление токена
- CRUD-операции над VPN-пользователями

### `bot.py`

- `/start` — приветствие
- `/login` — генерация кода, создание пользователя в БД
- `/help` — справка

### `main.py`

- Публичные страницы: `/`, `/login`, `/logout`
- Личный кабинет: `/cabinet`
- Админ-панель: `/admin`, `/admin/link`, `/admin/toggle`, `/admin/make-admin`, `/admin/delete`, `/admin/create-vpn`
