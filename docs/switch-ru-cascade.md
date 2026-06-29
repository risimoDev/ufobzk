# Переключение каскадного RU-сервера (быстро, без обрушения)

Каскад работает так: основной **NL**-сервер (и каждая remote-нода) держит
outbound `RU-PROXY` → `RU:443` поверх REALITY. Российский трафик (`.ru/.su`,
`geoip:ru`) уходит через RU-exit. Адрес и REALITY-ключи RU берутся из `.env`
основного сервера **на старте приложения** (`RU_SERVER_IP`, `RU_TRANSIT_*`).

Поэтому переключение = поднять новый RU-exit → обновить `.env` → пересоздать
только `ufo-app`. Единственный «простой» — краткий рестарт xray на NL
(несколько секунд). Делайте в час низкой нагрузки.

---

## Шаг 1. Поднять новый RU-exit

На новом RU-сервере (root). Сначала возьмите с основного сервера два значения:

```bash
# на NL (основном):
grep -E '^REALITY_PUBLIC_KEY=|^RU_TRANSIT_UUID=' .env
```

Затем на новом RU:

```bash
git clone https://github.com/risimoDev/ufobzk.git /opt/ufobzk
cd /opt/ufobzk
bash scripts/setup-ru-server.sh
#  → спросит: NL REALITY Public Key  (REALITY_PUBLIC_KEY с NL)
#  → спросит: Transit UUID           (RU_TRANSIT_UUID с NL)
#  → сгенерит и НАПЕЧАТАЕТ в конце:
#       RU REALITY Public:  <запомнить>
#       RU Short ID:        <запомнить>
#       serverName/SNI:     www.samsung.com (по умолчанию)
```

Откройте на новом RU порт 443 для NL (скрипт обычно делает это сам через UFW).

## Шаг 2. Переключить основной сервер

На NL (основном) одной командой:

```bash
bash scripts/12-switch-ru-cascade.sh \
    --ip 195.208.2.138 \
    --pubkey aTJP4DIeIAVyXUjQuYv4DHBjdVCuw4SlGs4ysyEMfx8 \
    --shortid b3cedd8dfb1068fe \
    --sn www.samsung.com
```

Скрипт: проверит TCP-доступность нового RU, забэкапит `.env`, обновит
`RU_SERVER_IP/RU_TRANSIT_*`, пересоздаст `ufo-app` (xray подхватит новый
RU-outbound), проверит что новый IP попал в `config.json`.

> Если RU-сервер использует другой transit-UUID — добавьте `--uuid <...>`.
> Если REALITY на нестандартном порту — `--port <...>`.

## Шаг 3. Проверить и погасить старый RU

1. Remote-ноды подхватят новый RU автоматически в течение ≤5 минут
   (периодическая синхронизация). Форсировать можно рестартом `ufo-app`.
2. На клиенте откройте российский сайт и проверьте выходной IP
   (например, через `https://2ip.ru` при заходе на `.ru`-домен) — должен
   стать новым RU IP.
3. Убедившись, что всё работает 10–15 минут — гасите старый RU-сервер.

## Откат

```bash
cp .env.bak.<timestamp> .env && docker compose up -d ufo-app
```

## Почему это «без обрушения»

- Пересоздаётся **только** `ufo-app`; `nginx`, сертификаты, БД не трогаются.
- Прямые подключения пользователей (WS/XHTTP/gRPC/REALITY к NL) не зависят от
  RU — рвётся лишь короткий рестарт xray. Сам RU-каскад влияет только на
  маршрут российских доменов.
- `.env` бэкапится, откат — одна команда.
