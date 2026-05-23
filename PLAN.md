# ПЛАН УЛУЧШЕНИЙ UFOBZK

> Документ актуален на 2026-05-24. Все изменения вносятся на локальной машине,
> затем деплоятся через `deploy.sh`. Каждая фаза — отдельный коммит + деплой.
> Правило безопасного деплоя: **ни одна фаза не ломает работающий прокси**.

---

## КРИТИЧЕСКАЯ НАХОДКА (исправляется в Фазе 1)

В `app/xray.py:724` вызывается:
```
xray api statsquery --server=127.0.0.1:10085
```
Но:
1. Бинарник `xray` **не установлен** в app-контейнере (`python:3.12-slim`)
2. `127.0.0.1:10085` **недостижим** из app-контейнера — это порт xray-контейнера

**Результат:** `data_used` у всех пользователей всегда = 0. Лимиты трафика не работают.

---

## СТАТУС РЕАЛИЗАЦИИ

| Фаза | Название | Статус |
|------|----------|--------|
| 1 | Fix Traffic Stats | ✅ done |
| 2 | SQLite WAL Mode | ✅ done |
| 3 | Sub-token Rotation | ✅ done |
| 4 | Auto DB Backup | ✅ done |
| 5 | Traffic Snapshots | ✅ done |
| 6 | Full Analytics Dashboard | ✅ done |
| 7 | BBR sysctl Script | ✅ done |
| 8 | Nginx Improvements | ✅ done |
| 9 | Unique Node Tokens UI | ✅ done |

---

## ФАЗА 1 — FIX TRAFFIC STATS

**Приоритет:** Критично | **Downtime:** ~2 мин (rebuild app-контейнера)

### Проблема
- `xray` бинарник отсутствует в `python:3.12-slim`
- Адрес `127.0.0.1:10085` — localhost app-контейнера, не xray-контейнера
- Xray-контейнер доступен внутри docker-сети по hostname `xray:10085`
- Текущий код делает N subprocess-вызовов (по одному на каждый ключ)

### Изменения

#### `Dockerfile`
Добавить multi-stage build — копировать xray бинарник из официального образа:
```dockerfile
FROM teddysun/xray:latest AS xray-source
FROM python:3.12-slim

WORKDIR /project
COPY --from=xray-source /usr/bin/xray /usr/bin/xray   # <-- добавить эту строку

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# остальное без изменений
```

#### `app/xray.py`
1. Константа адреса: добавить `XRAY_API_HOST = os.getenv("XRAY_API_HOST", "xray:10085")`
2. Удалить функцию `get_xray_stats(uuid)` (O(n) вызовов)
3. Добавить функцию `get_all_xray_stats() -> dict[str, int]`:
   - Один вызов: `xray api statsquery --server=xray:10085 -pattern=user>>>`
   - Парсинг protobuf-text вывода: `name: "user>>>UUID@tag>>>traffic>>>downlink"` + `value: 12345`
   - Возвращает `{uuid: total_bytes}` для всех пользователей

Формат вывода xray statsquery:
```
stat:  {
  name:  "user>>>UUID@VLESS-WS>>>traffic>>>uplink"
  value:  12345
}
stat:  {
  name:  "user>>>UUID@VLESS-WS>>>traffic>>>downlink"
  value:  67890
}
```

Логика парсинга: суммируем uplink+downlink по всем тегам для каждого UUID.

#### `app/tasks.py`
Заменить цикл O(n) на batch-вызов:
```python
# БЫЛО: for key in keys: stats = await asyncio.to_thread(get_xray_stats, key.uuid)
# СТАЛО:
all_stats = await asyncio.to_thread(get_all_xray_stats)
for key in keys:
    total = all_stats.get(key.uuid, 0)
    if total > (key.data_used or 0):
        key.data_used = total
        updated += 1
```

### Деплой
```bash
git pull
docker compose build ufo-app   # только app-контейнер
docker compose up -d ufo-app   # xray и nginx не трогаем
```

---

## ФАЗА 2 — SQLite WAL MODE

**Приоритет:** Высокий | **Downtime:** ~10 сек (app restart)

### Проблема
При конкурентной записи (3 фоновые задачи + HTTP запросы) SQLite без WAL
выдаёт `database is locked`. WAL (Write-Ahead Logging) решает это.

### Изменения

#### `app/models.py` — строки 27-28
```python
# БЫЛО:
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

# СТАЛО:
from sqlalchemy import event

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)

@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")       # concurrent reads+writes
    cur.execute("PRAGMA synchronous=NORMAL")     # быстрее, безопасно с WAL
    cur.execute("PRAGMA busy_timeout=30000")     # 30 сек ожидание вместо ошибки
    cur.execute("PRAGMA cache_size=-32000")      # 32MB page cache
    cur.execute("PRAGMA temp_store=MEMORY")      # temp tables в RAM
    cur.close()
```

### Деплой
```bash
docker compose restart ufo-app
```
WAL-файл (`vpnbzk.db-wal`) создаётся автоматически, обратно совместим.

---

## ФАЗА 3 — SUB-TOKEN ROTATION

**Приоритет:** Высокий (безопасность) | **Downtime:** ~10 сек

### Проблема
`sub_token` статичен навсегда. Если ссылка подписки утекла — доступ не отозвать.
Нужна кнопка "Обновить ссылку подписки" в кабинете.

### Изменения

#### `alembic/versions/006_add_sub_token_updated_at.py`
```python
def upgrade():
    op.add_column("users",
        sa.Column("sub_token_updated_at", sa.DateTime, nullable=True))

def downgrade():
    op.drop_column("users", "sub_token_updated_at")
```

#### `app/models.py`
Добавить в класс `User`:
```python
sub_token_updated_at = Column(DateTime, nullable=True)
```

#### `app/routers/cabinet.py`
Новый endpoint:
```python
@router.post("/cabinet/rotate-sub-token")
async def rotate_sub_token(
    request: Request,
    csrf_token: str = Form(""),
    user: User = Depends(_get_current_user),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    import secrets
    user.sub_token = secrets.token_urlsafe(32)
    user.sub_token_updated_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(url="/cabinet", status_code=303)
```

#### `templates/cabinet.html`
В блок с subscription URL добавить форму:
```html
<form method="POST" action="/cabinet/rotate-sub-token">
  <input type="hidden" name="csrf_token" value="{{ csrf_token }}">
  <button type="submit" onclick="return confirm('Старая ссылка перестанет работать!')">
    🔄 Обновить ссылку подписки
  </button>
</form>
```

---

## ФАЗА 4 — АВТО-БЭКАП БД

**Приоритет:** Высокий (надёжность) | **Downtime:** нет

### Стратегия
- Раз в 24 часа: копировать `vpnbzk.db` локально в `data/backups/`
- Хранить последние 7 копий, старые удалять
- Отправлять файл в Telegram (чат с ботом или личный чат суперадмина)
- Только чтение БД → нет риска повреждения

### Изменения

#### `app/backup.py` (новый файл)
```python
"""Автоматический бэкап SQLite БД — локально + Telegram."""
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

DB_PATH = Path("data/vpnbzk.db")
BACKUP_DIR = Path("data/backups")
KEEP_BACKUPS = 7
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SUPERADMIN_CHAT_ID = os.getenv("SUPERADMIN_TELEGRAM_ID", "")


def create_local_backup() -> Path:
    """Копирует БД в data/backups/, возвращает путь к файлу."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"vpnbzk_{ts}.db"
    shutil.copy2(DB_PATH, dst)
    _cleanup_old_backups()
    logger.info("Backup created: %s (%d bytes)", dst, dst.stat().st_size)
    return dst


def _cleanup_old_backups():
    backups = sorted(BACKUP_DIR.glob("vpnbzk_*.db"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink()
        logger.info("Deleted old backup: %s", old)


async def send_backup_to_telegram(filepath: Path) -> bool:
    if not TELEGRAM_TOKEN or not SUPERADMIN_CHAT_ID:
        logger.warning("Telegram backup skipped: BOT_TOKEN or CHAT_ID not set")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(filepath, "rb") as f:
                resp = await client.post(url, data={
                    "chat_id": SUPERADMIN_CHAT_ID,
                    "caption": f"🗄 Бэкап БД {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
                }, files={"document": (filepath.name, f, "application/octet-stream")})
        if resp.status_code == 200:
            logger.info("Backup sent to Telegram")
            return True
        logger.error("Telegram backup failed: %s", resp.text[:200])
        return False
    except Exception as e:
        logger.error("Telegram backup error: %s", e)
        return False
```

#### `app/tasks.py`
Добавить задачу и импорт:
```python
BACKUP_INTERVAL = 86400  # 24 часа

async def periodic_backup() -> None:
    await asyncio.sleep(3600)  # первый бэкап через час после старта
    while True:
        try:
            from app.backup import create_local_backup, send_backup_to_telegram
            filepath = await asyncio.to_thread(create_local_backup)
            await send_backup_to_telegram(filepath)
        except Exception as e:
            logger.error("Backup task error: %s", e)
        await asyncio.sleep(BACKUP_INTERVAL)
```

Добавить `periodic_backup()` в `run_background_tasks()`.

---

## ФАЗА 5 — ИСТОРИЯ ТРАФИКА (TrafficSnapshot)

**Приоритет:** Средний | **Downtime:** ~10 сек

### Цель
Хранить ежечасные дельты потребления трафика. Нужно для:
- Графиков потребления в аналитике (Фаза 6)
- Детектирования аномалий (резкий скачок трафика)
- Отчётов за период

### Изменения

#### `alembic/versions/007_add_traffic_snapshots.py`
```python
def upgrade():
    op.create_table("traffic_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("vpn_key_id", sa.Integer, sa.ForeignKey("vpn_keys.id", ondelete="CASCADE")),
        sa.Column("bytes_delta", sa.BigInteger, default=0),   # прирост за период
        sa.Column("total_bytes", sa.BigInteger, default=0),   # кумулятив на момент записи
        sa.Column("recorded_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_snapshots_key_time", "traffic_snapshots", ["vpn_key_id", "recorded_at"])
```

#### `app/models.py`
```python
class TrafficSnapshot(Base):
    __tablename__ = "traffic_snapshots"
    id = Column(Integer, primary_key=True)
    vpn_key_id = Column(Integer, ForeignKey("vpn_keys.id", ondelete="CASCADE"), index=True)
    bytes_delta = Column(BigInteger, default=0)
    total_bytes = Column(BigInteger, default=0)
    recorded_at = Column(DateTime, nullable=False,
                         default=lambda: datetime.now(timezone.utc), index=True)

    vpn_key = relationship("VPNKey", backref="snapshots")
```

#### `app/tasks.py` — модификация `periodic_traffic_collector()`
При каждом обновлении добавлять snapshot:
```python
from app.models import TrafficSnapshot

delta = new_total - (key.data_used or 0)
if delta > 0:
    key.data_used = new_total
    db.add(TrafficSnapshot(
        vpn_key_id=key.id,
        bytes_delta=delta,
        total_bytes=new_total,
    ))
    updated += 1
```

---

## ФАЗА 6 — FULL ANALYTICS DASHBOARD

**Приоритет:** Средний | **Downtime:** ~10 сек

### Цель
Полноценная аналитика в админ-панели:
- Сводная статистика (активные пользователи, трафик, ключи)
- График трафика по дням (Chart.js)
- Топ пользователей по трафику
- Активность по времени суток
- История синхронизаций серверов
- Статистика по протоколам

### Новые API endpoints в `app/routers/admin.py`

#### `GET /admin/api/analytics/summary`
```json
{
  "total_users": 42,
  "active_users": 38,
  "total_traffic_gb": 1234.5,
  "traffic_last_7d_gb": 89.2,
  "active_keys": 51,
  "expired_keys": 3,
  "limited_keys": 1,
  "servers_ok": 2,
  "servers_error": 0
}
```

#### `GET /admin/api/analytics/traffic?days=30`
```json
{
  "labels": ["2026-05-01", "2026-05-02", ...],
  "datasets": [
    {
      "label": "Трафик всего (GB)",
      "data": [12.3, 15.6, ...]
    }
  ]
}
```
Источник: агрегация `TrafficSnapshot` по дням.

#### `GET /admin/api/analytics/top-users?limit=10`
```json
[
  {"display_name": "Иван", "traffic_gb": 45.2, "keys_count": 2},
  ...
]
```

#### `GET /admin/api/analytics/server-stats`
```json
[
  {
    "name": "FI-Node",
    "region": "fi",
    "last_sync": "2026-05-24T12:00:00Z",
    "last_sync_status": "ok",
    "uptime_pct": 99.8
  }
]
```

### UI в `templates/admin.html`

Новая вкладка "📊 Аналитика" с разделами:

#### Сводные карточки (KPI блок)
```
┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ 42           │ │ 1.2 TB       │ │ 51 ключей    │ │ 2/2 серверов │
│ Пользователей│ │ Трафик всего │ │ Активных     │ │ Онлайн       │
└──────────────┘ └──────────────┘ └──────────────┘ └──────────────┘
```

#### График трафика (линейный, Chart.js)
- X: даты (последние 7/14/30 дней — переключатель)
- Y: GB в день
- Tooltip: точное значение

#### Топ-10 пользователей (таблица + mini bar chart)
- Имя, потреблено GB, % от общего, статус ключей

#### График по часам суток (bar chart)
- Активность: когда больше всего трафика
- Источник: `recorded_at` из `TrafficSnapshot` → группировка по часу

#### Серверы (health dashboard)
- Карточки серверов: имя, регион, последний sync, статус, время ответа

#### Журнал событий (audit timeline)
- Улучшенная версия текущего audit log
- Фильтрация по типу события
- Визуальная timeline

### Библиотеки (CDN, без установки)
```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
```
Chart.js уже используется в проекте (qrcode), добавить только chart.js.

---

## ФАЗА 7 — BBR + TCP ОПТИМИЗАЦИЯ

**Приоритет:** Средний (прокси производительность) | **Downtime:** нет

### Что даёт BBR
- +20-40% throughput на WAN соединениях
- Значительно лучше работает на линках с потерями пакетов
- Применяется на хосте, затрагивает все контейнеры

### `scripts/11-optimize-sysctl.sh` (новый скрипт)
```bash
#!/bin/bash
set -e
echo "=== Применение сетевых оптимизаций ==="

# Проверка что BBR поддерживается ядром
if ! modprobe tcp_bbr 2>/dev/null; then
    echo "WARN: BBR не поддерживается ядром, пропускаем"
fi

SYSCTL_FILE="/etc/sysctl.d/99-vpn-optimizations.conf"
cat > "$SYSCTL_FILE" << 'EOF'
# BBR congestion control (лучший алгоритм для VPN трафика)
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr

# TCP буферы (для высокоскоростных соединений)
net.core.rmem_max=134217728
net.core.wmem_max=134217728
net.ipv4.tcp_rmem=4096 87380 67108864
net.ipv4.tcp_wmem=4096 65536 67108864

# Очередь соединений
net.core.somaxconn=65535
net.ipv4.tcp_max_syn_backlog=65535
net.core.netdev_max_backlog=65535

# TIME_WAIT оптимизация
net.ipv4.tcp_tw_reuse=1
net.ipv4.tcp_fin_timeout=15

# UDP буфер (для QUIC/Hysteria2 в будущем)
net.core.rmem_default=26214400
net.core.wmem_default=26214400
EOF

sysctl -p "$SYSCTL_FILE"
echo "=== Готово. Проверка: ==="
sysctl net.ipv4.tcp_congestion_control
sysctl net.core.default_qdisc
```

### Деплой
```bash
# Запустить один раз на сервере:
bash scripts/11-optimize-sysctl.sh
# Настройки сохраняются после перезагрузки
```

---

## ФАЗА 8 — NGINX IMPROVEMENTS

**Приоритет:** Низкий | **Downtime:** ~2 сек (nginx reload)

### Изменения в `nginx/nginx.conf`

1. **SSL Resolver** (для OCSP stapling):
```nginx
resolver 1.1.1.1 8.8.8.8 valid=300s;
resolver_timeout 5s;
```

2. **Усилить HSTS**:
```nginx
# Было:
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
# Стало:
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
```

3. **X-Frame-Options строже**:
```nginx
# Было: SAMEORIGIN
add_header X-Frame-Options "DENY" always;
```

4. **Gzip для API**:
```nginx
gzip on;
gzip_comp_level 4;
gzip_types application/json text/plain text/css application/javascript;
gzip_min_length 1024;
gzip_vary on;
```

5. **Явные таймауты proxy**:
```nginx
proxy_connect_timeout 10s;
proxy_send_timeout    60s;
# proxy_read_timeout уже 120s для /admin — оставить
```

6. **Убрать лишний заголовок** `X-XSS-Protection` (устарел, CSP лучше).

### Деплой
```bash
docker exec ufobzk-nginx nginx -t && docker exec ufobzk-nginx nginx -s reload
```

---

## ФАЗА 9 — UNIQUE NODE TOKENS UI

**Приоритет:** Средний (безопасность) | **Downtime:** нет

### Проблема
При добавлении сервера admin вручную вводит токен — обычно используется один
`XRAY_NODE_TOKEN` на все ноды. Если одна нода скомпрометирована — атакующий
получает доступ к API всех остальных.

### Изменения

#### `app/routers/admin.py`
Новый endpoint для генерации токена:
```python
@router.get("/admin/api/generate-token")
async def generate_node_token(admin: User = Depends(require_admin)):
    import secrets
    return JSONResponse({"token": secrets.token_hex(32)})
```

#### `templates/admin.html`
В форму добавления/редактирования сервера — кнопка "Сгенерировать":
```html
<div class="flex gap-2">
  <input type="text" name="api_token" id="api_token" placeholder="Bearer токен" ...>
  <button type="button" id="gen-token-btn" onclick="generateToken()">
    🎲 Генерировать
  </button>
</div>
<script>
async function generateToken() {
  const r = await fetch('/admin/api/generate-token');
  const d = await r.json();
  document.getElementById('api_token').value = d.token;
}
</script>
```

---

## ПРАВИЛА ДЕПЛОЯ

### Безопасная последовательность для каждой фазы:
```bash
# 1. Проверить что прокси работает до деплоя
curl -s https://ufolabs.online/health

# 2. Залить изменения
git pull origin main

# 3. Применить (зависит от фазы — см. выше)
docker compose up -d --no-deps ufo-app  # без rebuild
# ИЛИ
docker compose build ufo-app && docker compose up -d --no-deps ufo-app  # с rebuild

# 4. Проверить что прокси работает после
curl -s https://ufolabs.online/health
docker logs ufobzk-app --tail=50
```

### Откат при проблемах:
```bash
git revert HEAD
docker compose build ufo-app && docker compose up -d --no-deps ufo-app
```

---

## БУДУЩИЕ УЛУЧШЕНИЯ (не входят в план)

- **Hysteria2** — UDP-протокол, +3-10x скорость на нестабильных соединениях
- **PostgreSQL** — замена SQLite при росте >500 пользователей
- **REALITY destination rotation** — пул SNI вместо одного samsung.com
- **Prometheus + Grafana** — внешний мониторинг
- **Per-user bandwidth throttling** — ограничение скорости в Xray policy
