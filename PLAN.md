# VPNBZK — План разработки v2

> Дата: 2026-05-24  
> Статус: В работе  
> Правило: **ни одна фаза не ломает работающий прокси**

---

## Диагностика: почему не работала аналитика

1. **Chart.js CDN заблокирован** — `cdn.jsdelivr.net` недоступен в ряде сетей/РФ → переход на self-hosted
2. **JS ошибки молча глотались** — `catch(e) { console.error(...) }` без UX-обратной связи
3. **Пустая таблица `traffic_snapshots`** — сразу после деплоя нет данных; нужно сообщение «данные ещё не собраны»
4. **Нет способа проверить сбор трафика** без ожидания 5 минут — нужна кнопка «Собрать сейчас»

---

## ✅ Фаза 1–9 (предыдущая сессия — завершены)

- Phase 1: Исправлены stats Xray (multi-stage Dockerfile + `get_all_xray_stats()`)
- Phase 2: SQLite WAL mode (5 PRAGMA, busy_timeout 30s)
- Phase 3: Ротация sub_token (migration 006 + endpoint + cabinet UI)
- Phase 4: Auto DB backup (backup.py + periodic_backup task)
- Phase 5: TrafficSnapshot модель + migration 007 + collector пишет дельты
- Phase 6: Аналитика — 4 API endpoints + Chart.js tab в admin.html
- Phase 7: BBR sysctl скрипт
- Phase 8: Nginx: gzip, modern TLS ciphers, HSTS preload, resolver, DENY frame
- Phase 9: UI генерации токенов для серверов

---

## ✅ Фаза 10 — Починить аналитику (текущая сессия)

### 10.1 Self-host Chart.js
- [x] Скачать `chart.umd.min.js` v4.4.0 в `app/static/js/`
- [x] Заменить CDN → `/static/js/chart.umd.min.js` в admin.html

### 10.2 UX аналитики
- [x] Loading spinner в analytics tab
- [x] Видимое сообщение об ошибке (не только console.error)
- [x] «Данные ещё не собраны» когда `snapshot_count == 0`
- [x] Кнопка «Собрать сейчас» → POST `/admin/api/analytics/force-collect`

### 10.3 Backend
- [x] Поле `snapshot_count` в `/admin/api/analytics/summary`
- [x] Endpoint `POST /admin/api/analytics/force-collect`

---

## ✅ Фаза 11 — QR-коды (текущая сессия)

- [x] qrcode.min.js уже в `/static/`
- [x] Кнопка «QR» в cabinet.html для ссылки подписки
- [x] Модальное окно с QR-кодом

---

## ✅ Фаза 12 — UX кабинета (текущая сессия)

- [x] Жёлтый бейдж «X дней» когда ключ истекает ≤7 дней
- [x] Оранжевый бейдж «X MB осталось» когда использовано ≥80% лимита
- [x] Кнопка «Сбросить трафик» в admin.html для ключей

---

## Фаза 13 — Улучшения прокси (следующие сессии)

### 13.1 Subscription headers (Clash/Hiddify userinfo)
- В `/sub/{token}` добавить заголовки:
  - `Subscription-Userinfo: upload=0; download=X; total=Y; expire=Z`
  - `Profile-Title: VPNBZK`
- Клиенты автоматически показывают остаток трафика

### 13.2 Hysteria2 транспорт (UDP, очень быстрый)
- Добавить Hysteria2 inbound в xray config (порт 8446 UDP)
- Открыть UDP 8446 в docker-compose
- Добавить ссылку Hysteria2 в VPN links

### 13.3 Per-client speed limit
- Поле `speed_limit_mbps` в VPNKey (nullable)
- Миграция 008
- В xray config: `limitSpeed` в клиенте если задан

### 13.4 Улучшенный мониторинг
- Active connections per UUID из Xray stats API
- Показывать в admin для каждого ключа: «онлайн/оффлайн»

---

## Деплой-инструкция

```bash
# 1. Пересобрать image (ОБЯЗАТЕЛЬНО — новые static files)
docker compose build ufo-app

# 2. Перезапустить app без downtime
docker compose up -d --no-deps ufo-app

# 3. Перезагрузить nginx (если менялся nginx.conf)
docker exec ufobzk-nginx nginx -s reload
```

> Alembic миграции применяются автоматически при старте app.  
> BBR (`scripts/11-optimize-sysctl.sh`) — запустить один раз на хосте от root.
