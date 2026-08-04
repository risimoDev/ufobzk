# Руководство по развертыванию VPNBZK Cascade

## Архитектура

```
┌──────────────────────────────────────┐
│         Основной сервер              │
│  ┌──────────┐  ┌──────────────────┐  │
│  │  Nginx   │  │  ufo-app (FastAPI)│  │
│  │  80/443  │  │     8000         │  │
│  └────┬─────┘  └──────────────────┘  │
│       │                              │
│  ┌────┴──────┐  ┌─────────────────┐  │
│  │  Xray     │  │  SQLite (data/) │  │
│  │  REALITY  │  │                 │  │
│  └───────────┘  └─────────────────┘  │
└──────────┬───────────────────────────┘
           │ POST /config + Bearer token
           ▼
┌──────────────────────────────────────┐
│      Remote Xray-node серверы        │
│  ┌──────────┐  ┌──────────────────┐  │
│  │  xray-node│  │  Xray core      │  │
│  │  FastAPI  │  │  (docker/svc)   │  │
│  └───────────┘  └──────────────────┘  │
└──────────────────────────────────────┘
```

---

## 1. Подготовка серверов

Ваша инфраструктура:

| Сервер      | IP               | Роль                                 | Порты         |
| ----------- | ---------------- | ------------------------------------ | ------------- |
| Основной NL | `66.248.207.111` | ufo-app + Xray + Nginx               | 80, 443, 2053 |
| RU          | `193.163.203.40` | Xray-node (systemd Xray)             | 443, 9090 API |
| Доп. NL     | `91.229.105.51`  | Xray-node (сторонние Docker проекты) | 443, 9090 API |
| Finland     | `217.60.60.51`   | Xray-node (сторонние проекты)        | 443, 8080 API |

### Требования к основному серверу

- **OS**: Ubuntu 22.04/24.04 LTS
- **RAM**: 2 GB+
- **Диск**: 20 GB+
- **Порты**: 22 (SSH), 80 (HTTP), 443 (HTTPS), 2053 (Xray REALITY)
- **Домен**: `ufolabs.online` → A-запись на `66.248.207.111`

### Требования к Xray-node серверам

- **OS**: Ubuntu 22.04/24.04 LTS
- **RAM**: 1 GB+
- **Порты**: 22 (SSH), 443 (Xray VPN), API порт (9090/8080 — зависит от сервера)
- Доступ до основного сервера (`66.248.207.111`) по сети

### Установка Docker и Docker Compose

На **каждом** сервере:

```bash
# Обновление
sudo apt update && sudo apt upgrade -y

# Docker
sudo apt install -y apt-transport-https ca-certificates curl gnupg lsb-release
sudo mkdir -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# Docker без sudo (опционально)
sudo usermod -aG docker $USER
newgrp docker

# Проверка
docker --version
docker compose version
```

---

## 2. Основной сервер

### 2.1. Клонирование и настройка

```bash
cd /opt
sudo git clone <URL репозитория> ufobzk
sudo chown -R $USER:$USER ufobzk
cd ufobzk
```

### 2.2. Переменные окружения (`.env`)

Создайте файл `/opt/ufobzk/.env`:

```dotenv
# ── Домен и SSL ──
DOMAIN=ufolabs.online
EMAIL=levrurisimo@gmail.com

# ── Секреты приложения ──
SECRET_KEY=cf52dc2ea4105fa090fffb45fca77c25ea67c784c279f607ea4ede6329a88ceb

# ── Telegram Bot ──
TELEGRAM_BOT_TOKEN=8010034531:AAGyTiAMxYrmN8rks7oW8Y2mQ7I3cgeIQ7o
TELEGRAM_BOT_USERNAME=ufo_comunitybot
WEBAPP_URL=https://ufolabs.online

# ── Каскадные серверы ──
NL_SERVER_IP=66.248.207.111
RU_SERVER_IP=193.163.203.40

# ── Xray ──
XRAY_CONFIG_PATH=/etc/xray/config.json
VLESS_WS_PORT=443
REALITY_PORT=2053

# ── REALITY (заполняется скриптом 05-setup-reality.sh) ──
REALITY_PUBLIC_KEY=NG63mjKr8RNFKeJSEyoqOBW_UwPos8YGpqbkfamc60M
REALITY_PRIVATE_KEY=uMtnwXWJdA3yicCZql_HFbp2V8CEYChUJJBhOHvbVXI
REALITY_SHORT_ID=c21c6ddc00a0d49b
REALITY_DEST=www.samsung.com:443
REALITY_SERVER_NAMES=www.samsung.com,samsung.com

# ── Доступ ──
ADMIN_IPS=85.17.162.53
SUPERADMIN_TELEGRAM_ID=757479170
ADMIN_PASSWORD=Ro3CoQmHP6MFV979oQCiG5C5T5k

# ── Xray Node Token ──
# Токен для авторизации remote xray-node серверов (RU, доп. NL, Finland).
# Должен быть одинаковым на всех нодах. Сгенерируйте случайный 64 hex.
XRAY_NODE_TOKEN=<GENERATE_AND_USE_SAME_ON_ALL_NODES>

RU_TRANSIT_UUID=4da8a005-5078-432d-829d-3c4fb61ecbd5
RU_TRANSIT_PORT=8443
RU_TRANSIT_PUBLIC_KEY=KGNL969Z735wdKu-Uk-p6V3ubg_y_T1Q0qxAiAj6-jU
RU_TRANSIT_SHORT_ID=aabbccdd
RU_TRANSIT_SN=www.yandex.ru
```

**Как сгенерировать REALITY ключи:**

```bash
# На сервере с установленным xray:
./xray x25519
# Вывод:
# Private key: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
# Public key:  BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB
```

**Как сгенерировать SECRET_KEY:**

```bash
openssl rand -hex 32
```

### 2.3. Первый запуск (получение SSL)

На **первом** запуске нужно получить сертификат Let's Encrypt. Используется упрощенный nginx конфиг:

```bash
cd /opt/ufobzk

# Запускаем только certbot + nginx initial
docker compose -f docker-compose.yml up -d certbot nginx

# Ждем 30-60 секунд и проверяем, что сертификат получен
sudo ls /opt/ufobzk/certbot/conf/live/${DOMAIN}/
```

Если сертификат получен, перезапустите с полным nginx:

```bash
docker compose restart nginx
```

**Если проблемы с certbot** (брандмауэр, DNS не пропагировался):

- Убедитесь, что A-запись домена указывает на сервер
- Порт 80 должен быть открыт

### 2.4. Полный запуск всех сервисов

```bash
cd /opt/ufobzk
docker compose up -d --build

# Проверка статуса
docker compose ps
docker compose logs -f ufo-app
```

### 2.5. Проверка работы

```bash
# Health check приложения
curl -k https://localhost/api/health

# Проверка Xray
curl -k https://localhost:2053/  # должен вернуть пустой ответ или 400

# Admin panel
curl -k https://localhost/admin
```

### 2.6. MTProto-прокси для Telegram (mtg)

Отдельный прокси специально для Telegram (FakeTLS). Контейнер `mtg` уже в
`docker-compose.yml`, секрет генерируется из админки.

Образ mtg — distroless (нет `/bin/sh`), бинарь запускается напрямую
(`command: run /etc/mtg/config.toml`). Конфиг (секрет) пишет приложение в общий
том `mtg-config`, поэтому **секрет генерируем ДО подъёма mtg** — иначе контейнер
будет падать в restart-loop, пока файла нет.

```bash
# 1. Открыть порт MTProto в firewall (дефолт 8765, см. MTPROTO_PORT в .env)
ufw allow 8765/tcp comment 'MTProto (Telegram)'

# 2. Пересоздать ufo-app под новый compose (нужен общий том mtg-config)
docker compose up -d --build ufo-app

# 3. В админке: Настройки → 🔵 MTProto прокси → «Сгенерировать секрет».
#    Приложение запишет config.toml в том mtg-config (и попробует поднять mtg).

# 4. Поднять/перезапустить сервис — теперь config.toml уже есть
docker compose up -d mtg

# 5. Проверка
docker logs ufobzk-mtg          # должен слушать 0.0.0.0:3128
docker compose ps mtg
```

Ссылка `tg://proxy` / `https://t.me/proxy` появится в админке и в кабинете
пользователя. На главном host-порты 80/443/2053 заняты nginx/xray — поэтому
MTProto вынесен на отдельный порт (дефолт 8765, меняется через `MTPROTO_PORT`).

### 2.7. WARP-выход для Google/YouTube

**Симптом:** Google и YouTube считают выход российским, хотя сервер в NL:

```
Service                 IPv4    IPv6
Google                  RU      PL
YouTube                 RU      PL
Netflix                 NL      NL     ← все остальные видят NL
maxmind.com             NL      NL
rdap.db.ripe.net        PL      NL
```

**Причина.** Google не использует MaxMind и не читает whois — у него своя
гео-база, и главный её источник поведенческий: устройства на Android/Chrome
регулярно отправляют пару «GPS-координаты + текущий IP». Через наш IPv4 ходят
почти только российские пользователи, поэтому Google обучил базу считать этот
/24 российским. IPv6 используется мало и пока «чистый» — отсюда PL.

Следствия: геофид (RFC 8805) и правка `country:` в RIPE не помогают (иначе
Google показывал бы PL, как и RIPE), а смена IP даёт лишь отсрочку — новый
адрес протухнет так же, как только на него сядут те же пользователи.

**Решение** — выпускать google/youtube через Cloudflare WARP.

Сначала проверьте, что WARP вообще пропускает трафик (поднимает отдельный
временный контейнер, прод не трогает):

```bash
bash scripts/15-diagnose-warp.sh
```

И только если он сказал «WARP пропускает трафик» — включайте:

```bash
bash scripts/06-setup-warp.sh
```

Откат в одну команду, если что-то пошло не так:

```bash
bash scripts/06-setup-warp.sh --disable
```

Скрипт ставит `wgcf`, регистрирует аккаунт WARP, вычисляет `reserved` из
`client_id` и пишет `WARP_*` в `.env`. Конфиг Xray собирает приложение
(`app/xray.py` → `build_xray_config`), поэтому руками `/etc/xray/config.json`
править нельзя — затрётся при следующем `sync_and_reload`. По той же причине
скрипт делает `up -d --build`: `Dockerfile` копирует код в образ (`COPY . .`),
и без пересборки контейнер поднимется со старым `app/xray.py`.

WARP включается, только когда заданы `WARP_PRIVATE_KEY` + `WARP_PUBLIC_KEY` +
`WARP_ADDRESS_V4`. Список доменов — `WARP_DOMAINS` в `.env`; дефолт в
`app/xray.py` покрывает YouTube, поиск (`www.google.com`), Gemini
(`gemini.google.com`) и Antigravity (`antigravity.google` + `codeium.com`).

Про Antigravity — трафик расходится по трём направлениям, и заворачивать нужно
все:

| Куда | Что это |
| --- | --- |
| `antigravity.google` | сайт и вход |
| `codeium.com` | служебные запросы IDE (построен на Windsurf/Codeium) |
| `cloudcode-pa` / `cloudaicompanion` / `generativelanguage` `.googleapis.com` | сами вызовы модели |

Если не завёрнуты последние, IDE подключается, но агент падает с
`HTTP 400 FAILED_PRECONDITION: User location is not supported for the API use`.
Признак того, что дело именно в них — заголовок `X-Cloudaicompanion-Trace-Id` в
ответе. Эндпоинты перечислены поимённо: `googleapis.com` целиком заворачивать
нельзя, туда попадают пуши и служебные каналы Android.

Какие домены сервис использует на самом деле, гадать не нужно — они видны в
access.log Xray:

```bash
bash scripts/16-warp-candidates.sh --watch 120
```

Скрипт собирает трафик указанное время (откройте нужный сервис на клиенте) и
показывает, какие google-домены ушли в `DIRECT`, а какие уже идут через `WARP`,
с готовыми строками для `WARP_DOMAINS`. Клиентские IP и email из лога не
выводятся. Без `--watch` разбирает весь накопленный лог.

Про Gemini отдельно: он гейтит доступ не только по IP запроса, но и по стране
самого Google-аккаунта. Страна аккаунта считается по длительной истории
использования и не переключается сразу после смены выхода. Поэтому чистый NL-IP
— условие необходимое, но не всегда достаточное: аккаунт с российской историей
может продолжать получать отказ.

> **Туннель WARP должен быть строго IPv4** (`WARP_IPV6=0`, эндпоинт — IPv4-литерал).
> Контейнер `ufobzk-xray` живёт в docker-сети `backend`, где IPv6 нет. Если
> объявить в туннеле адрес `/128` и `allowedIPs: ::/0`, Xray начинает резолвить
> домены через IPv6-резолвер `2606:4700:4700::1001` внутри туннеля и получает
> `i/o timeout`, а при AAAA-записи эндпоинта — `sendto: network is unreachable`.
> Наружу это выглядит так, будто WARP просто молча не пропускает трафик.

> **Не добавляйте в `WARP_DOMAINS` категорию `geosite:google` целиком.** В неё
> входят `connectivitycheck.gstatic.com`, `dns.google`, `googleapis.com` и
> `mtalk.google.com` (FCM-пуши). Android определяет наличие интернета запросом
> к connectivitycheck: если WARP не пропускает трафик, телефон помечает
> соединение как «без доступа в интернет» и приложения перестают работать при
> живом туннеле — выглядит как «VPN сломался». Держите список узким: YouTube и
> поиск, то есть ровно то, где мешает гео-метка.

> **Важно:** правило WARP ставится **выше** правил каскада. Google, считая нас
> RU, отдаёт для `*.googlevideo.com` адреса GGC-кэшей внутри российских AS —
> они матчатся на `geoip:ru`, и без этого правила YouTube уходил бы в RU-хаб,
> то есть выходил в реальный российский IP.

**Проверять результат нужно с клиента, а не с сервера.** `ipregion.sh` или
`curl`, запущенные в шелле сервера, идут через сетевой стек хоста мимо xray —
маршрут WARP применяется только к трафику, вошедшему через инбаунд. С сервера вы
всегда увидите прежнее `Google RU`, и это ничего не говорит о том, работает ли
фикс.

Подключитесь клиентом и выполните **на клиенте**:

```bash
curl -s https://www.youtube.com/ | grep -o '"INNERTUBE_CONTEXT_GL":".."'
```

Ожидается не `RU`. Cookies `google.com`/`youtube.com` в браузере сбросить:
русский интерфейс может остаться из-за старой сессии и заголовка
`Accept-Language: ru`, а не из-за IP.

Если трафик не идёт — `WARP_MTU=1420` или `WARP_ENDPOINT=162.159.192.1:2408`.
Если бесплатный WARP тормозит на 4K — лицензия WARP+:
`cd .warp && wgcf update --license 'XXXX-XXXX-XXXX' && bash scripts/06-setup-warp.sh`

### 2.8. Настройка Xray нод в админке

После первого запуска:

1. Откройте `https://ufolabs.online/admin`
2. Войдите с паролем из `ADMIN_PASSWORD`
3. Перейдите в раздел **Servers**
4. Добавьте remote серверы:

| Name        | Host             | Port   | Token             | Region | Is Active |
| ----------- | ---------------- | ------ | ----------------- | ------ | --------- |
| `ru-node-1` | `193.163.203.40` | `9090` | `XRAY_NODE_TOKEN` | `RU`   | ✅        |
| `nl-node-2` | `91.229.105.51`  | `9090` | `XRAY_NODE_TOKEN` | `NL`   | ✅        |
| `fi-node-1` | `217.60.60.51`   | `8080` | `XRAY_NODE_TOKEN` | `FI`   | ✅        |

> **Token** должен быть одинаковым на всех нодах и в `.env` основного сервера.
> Если `XRAY_NODE_TOKEN` не задан — скрипт `08-deploy-main-server.sh` сгенерирует его автоматически.

---

## 3. Xray-node серверы (Remote)

> **⚠️ Важно для серверов с существующими Docker-проектами**
>
> На доп. NL (`91.229.105.51`) порты 80/443/3000/3001/8080 заняты сторонними контейнерами.
> На Finland (`217.60.60.51`) порты 80/443 заняты nginx, 5432 — PostgreSQL.
>
> **Решение**: xray-node API запускается через **systemd** (не Docker), а Xray core ставится через `install-release.sh` (тоже systemd). Так нет конфликтов с существующими Docker-проектами.
>
> API порт выбираем свободный:
>
> - RU (`193.163.203.40`): **9090** (Xray уже установлен через `setup-ru-server.sh`)
> - Доп. NL (`91.229.105.51`): **9090** (8080 занят)
> - Finland (`217.60.60.51`): **8080** (свободен)

### 3.1. Быстрый деплой через скрипт (рекомендуется)

Скопируйте `scripts/09-deploy-xray-node.sh` на сервер и запустите:

#### RU сервер (`193.163.203.40`) — Xray уже установлен

```bash
# На основном сервере — копируем скрипт и запускаем через SSH
scp scripts/09-deploy-xray-node.sh root@193.163.203.40:/tmp/
ssh root@193.163.203.40 "bash /tmp/09-deploy-xray-node.sh 193.163.203.40 9090 --skip-xray-install --main-server 66.248.207.111"
```

#### Доп. NL сервер (`91.229.105.51`) — нужен Xray + xray-node

```bash
scp scripts/09-deploy-xray-node.sh root@91.229.105.51:/tmp/
ssh root@91.229.105.51 "bash /tmp/09-deploy-xray-node.sh 91.229.105.51 9090 --main-server 66.248.207.111"
```

#### Finland сервер (`217.60.60.51`) — нужен Xray + xray-node

```bash
scp scripts/09-deploy-xray-node.sh root@217.60.60.51:/tmp/
ssh root@217.60.60.51 "bash /tmp/09-deploy-xray-node.sh 217.60.60.51 8080 --main-server 66.248.207.111"
```

> Скрипт спросит `XRAY_NODE_TOKEN` (или сгенерирует новый). **Используйте один и тот же токен на всех нодах** и запишите его в `.env` основного сервера.

### 3.2. Ручная установка (если скрипт не подходит)

Если нужно вручную — повторите шаги скрипта:

1. **Установить Xray core** (если нет):

   ```bash
   bash -c "$(curl -L https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" @ install
   ```

2. **Клонировать репозиторий**:

   ```bash
   cd /opt && git clone <URL> ufobzk && cd ufobzk/xray-node
   ```

3. **Установить Python-зависимости**:

   ```bash
   pip3 install -r requirements.txt
   ```

4. **Создать `.env`**:

   ```bash
   cat > /opt/ufobzk/xray-node/.env <<EOF
   XRAY_NODE_TOKEN=<ТОТ_ЖЕ_ТОКЕН>
   XRAY_NODE_PORT=9090
   XRAY_CONFIG_PATH=/etc/xray/config.json
   XRAY_CONTAINER_NAME=xray
   EOF
   ```

5. **Systemd сервис**:

   ```bash
   cat > /etc/systemd/system/xray-node.service <<'EOF'
   [Unit]
   Description=UFOBZK Xray Node API
   After=network.target xray.service

   [Service]
   Type=simple
   User=root
   WorkingDirectory=/opt/ufobzk/xray-node
   Environment=XRAY_NODE_TOKEN=<ТОКЕН>
   Environment=XRAY_NODE_PORT=9090
   Environment=XRAY_CONFIG_PATH=/etc/xray/config.json
   Environment=XRAY_CONTAINER_NAME=xray
   ExecStart=/usr/bin/python3 -m uvicorn main:app --host 0.0.0.0 --port 9090
   Restart=always

   [Install]
   WantedBy=multi-user.target
   EOF

   systemctl daemon-reload
   systemctl enable xray-node
   systemctl start xray-node
   ```

6. **UFW**:
   ```bash
   ufw allow 443/tcp comment 'Xray VPN'
   ufw allow from 66.248.207.111 to any port 9090 proto tcp comment 'xray-node API only from main'
   ufw --force enable
   ```

---

## 4. Обновление после изменений кода

### 4.1. Обновление основного сервера

```bash
# Автоматически (рекомендуется):
bash scripts/08-deploy-main-server.sh

# Или вручную:
cd /opt/ufobzk
git pull origin main
docker compose down
docker compose up -d --build
docker compose exec ufo-app alembic upgrade head
docker image prune -f
```

### 4.2. Обновление Xray-node серверов

Все xray-node работают через **systemd** (чтобы не конфликтовать с Docker-проектами).

```bash
# Перезапустить на всех серверах (с основного NL сервера):
ssh root@193.163.203.40 "cd /opt/ufobzk/xray-node && git pull origin main && pip3 install -q -r requirements.txt && systemctl restart xray-node && systemctl restart xray"

ssh root@91.229.105.51 "cd /opt/ufobzk/xray-node && git pull origin main && pip3 install -q -r requirements.txt && systemctl restart xray-node && systemctl restart xray"

ssh root@217.60.60.51 "cd /opt/ufobzk/xray-node && git pull origin main && pip3 install -q -r requirements.txt && systemctl restart xray-node && systemctl restart xray"
```

### 4.3. Массовое обновление всех серверов

```bash
# Запустить на основном NL сервере:
bash scripts/10-update-all.sh

# Скрипт последовательно:
# 1. Обновляет основной сервер (git pull, docker compose rebuild, миграции)
# 2. Обновляет RU сервер через SSH (git pull, restart xray-node + xray)
# 3. Обновляет доп. NL сервер через SSH
# 4. Обновляет Finland сервер через SSH
# 5. Проверяет health API на каждой ноде
```

---

## 5. Полный список переменных окружения

| Переменная               | Обязательная | Описание                                    |
| ------------------------ | ------------ | ------------------------------------------- |
| `SECRET_KEY`             | ✅           | JWT / сессии (минимум 32 символа)           |
| `DOMAIN`                 | ✅           | Основной домен (например `vpn.example.com`) |
| `NL_SERVER_IP`           | ✅           | IP сервера в Нидерландах                    |
| `RU_SERVER_IP`           | ✅           | IP сервера в России                         |
| `ADMIN_PASSWORD`         | ❌           | Пароль для админ-панели                     |
| `SUPERADMIN_TELEGRAM_ID` | ❌           | Telegram ID суперадмина                     |
| `REALITY_PRIVATE_KEY`    | ❌           | X25519 private key для REALITY              |
| `REALITY_PUBLIC_KEY`     | ❌           | X25519 public key для REALITY               |
| `XRAY_CONTAINER_NAME`    | ❌           | Имя Docker-контейнера Xray (`xray`)         |
| `XRAY_NODE_TOKEN`        | ❌           | Токен авторизации remote нод                |
| `TELEGRAM_BOT_TOKEN`     | ❌           | Токен Telegram бота                         |
| `WEBAPP_URL`             | ❌           | URL приложения (генерируется из DOMAIN)     |
| `WARP_PRIVATE_KEY`       | ❌           | WireGuard private key WARP (см. 2.7)        |
| `WARP_PUBLIC_KEY`        | ❌           | Public key пира WARP                        |
| `WARP_ADDRESS_V4`        | ❌           | IPv4 внутри WARP (три вместе включают WARP) |
| `WARP_ADDRESS_V6`        | ❌           | IPv6 внутри WARP                            |
| `WARP_ENDPOINT`          | ❌           | `162.159.192.1:2408` — только IPv4-литерал  |
| `WARP_RESERVED`          | ❌           | 3 байта client_id, считает скрипт           |
| `WARP_MTU`               | ❌           | `1280`; если трафик не идёт — `1420`        |
| `WARP_IPV6`              | ❌           | `0`. Включать нельзя — см. 2.7              |
| `WARP_DOMAINS`           | ❌           | Что заворачивать в WARP                     |

---

## 6. Типичные проблемы

### SSL / Certbot

**Проблема**: `certbot` не может получить сертификат.
**Решение**:

```bash
# Проверьте DNS
dig +short your-domain.com
# Должен вернуть IP сервера

# Проверьте порт 80
sudo ss -tlnp | grep :80
# Должен слушать nginx

# Повторно запросить сертификат
docker compose restart certbot
docker compose logs certbot
```

**Проблема**: браузер пишет `ERR_CERT_DATE_INVALID` (сертификат просрочен).
**Решение**: обновить cert и ОБЯЗАТЕЛЬНО перезагрузить nginx — он держит
сертификат в памяти с момента старта и сам новый файл не подхватит:

```bash
cd /opt/ufobzk

# Что реально отдаётся наружу сейчас
echo | openssl s_client -connect ${DOMAIN}:443 -servername ${DOMAIN} 2>/dev/null \
  | openssl x509 -noout -dates

# Обновить. --entrypoint certbot обязателен: без него аргументы игнорируются
# и запускается вечный цикл вместо разового renew.
docker compose run --rm --entrypoint certbot certbot renew
docker compose exec nginx nginx -s reload

# Цикл автообновления должен быть Up, а не Exited
docker compose ps certbot
docker compose up -d certbot
```

### Xray не синхронизируется с нодами

**Проблема**: При добавлении пользователя конфиг не уходит на remote сервер.
**Решение**:

```bash
# На основном сервере — проверьте логи
docker compose logs -f ufo-app | grep xray

# На xray-node — проверьте доступность
curl -H "Authorization: Bearer <TOKEN>" http://<NODE_IP>:8080/health

# Должен вернуть {"status":"ok"}
```

### Nginx: 502 Bad Gateway

**Проблема**: `ufo-app` не отвечает.
**Решение**:

```bash
docker compose ps ufo-app
docker compose logs ufo-app --tail 50

# Проверьте, что приложение слушает на 0.0.0.0:8000
docker compose exec ufo-app ss -tlnp
```

### Xray-node: Permission denied при перезапуске Xray

**Проблема**: xray-node не может перезапустить контейнер.
**Решение**: Убедитесь, что `docker.sock` примонтирован:

```bash
docker inspect xray-node | grep -A 5 "Mounts"
```

### VPN «перестал работать» на телефонах после включения WARP

**Проблема**: туннель подключается, но на Android приложения не работают и
система показывает «нет доступа в интернет». На десктопе при этом заметно меньше.

**Причина**: на WARP заведены домены, среди которых `connectivitycheck.gstatic.com`
(проверка связности Android) или `mtalk.google.com` (FCM-пуши), а WARP трафик не
пропускает. Обычно так бывает, если в `WARP_DOMAINS` попала категория
`geosite:google` целиком.

**Решение** — сначала откат, потом разбор:

```bash
bash scripts/06-setup-warp.sh --disable   # вернуть прямой выход
bash scripts/15-diagnose-warp.sh          # проверить, живой ли WARP
```

Диагностика перебирает варианты (`reserved [0,0,0]`, `MTU 1420`, другие
endpoint'ы, IPv6 в туннеле) и печатает рабочие параметры для `.env`.

Самая частая причина — IPv6 в туннеле при docker-сети без IPv6; в логах видно
`sendto: network is unreachable` или `lookup ... on 2606:4700:4700::1001: i/o
timeout`. Лечится `WARP_IPV6=0` и IPv4-литералом в `WARP_ENDPOINT`.

Если не заработал ни один вариант — скорее всего хостер режет UDP 2408
(`nc -zvu 162.159.192.1 2408`) либо аккаунт WARP забанен для этого IP. Тогда
вместо WARP используйте цепочку на свою ноду.

---

## 7. Быстрые команды

```bash
# Статус всего стека
cd /opt/ufobzk && docker compose ps

# Логи в реальном времени
cd /opt/ufobzk && docker compose logs -f

# Перезапуск конкретного сервиса
cd /opt/ufobzk && docker compose restart ufo-app

# Бэкап базы данных
cp /opt/ufobzk/data/ufobzk.db /backup/ufobzk-$(date +%F).db

# Вход в контейнер приложения
cd /opt/ufobzk && docker compose exec ufo-app bash
```
