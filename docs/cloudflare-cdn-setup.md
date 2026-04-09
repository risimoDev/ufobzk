# Настройка Cloudflare CDN для маскировки VPN-трафика

> Если ваш IP-адрес сервера заблокирован или провайдер использует DPI —
> пропустите VPN-трафик через CDN Cloudflare. Для блокировщика это
> выглядит как обычный HTTPS-запрос к Cloudflare, а не к вашему серверу.

---

## Как это работает

```
┌─────────┐     HTTPS/443     ┌─────────────┐     HTTPS     ┌──────────┐
│ Клиент  │ ──────────────►   │ Cloudflare  │ ──────────►   │  Сервер  │
│ (v2ray) │  WebSocket/gRPC   │   CDN Edge  │  proxy_pass   │  nginx → │
│         │ ◄────────────────  │             │ ◄──────────   │  Xray    │
└─────────┘                   └─────────────┘               └──────────┘
     │                              │
     │  Провайдер видит HTTPS       │  Реальный IP сервера скрыт
     │  к IP Cloudflare             │  за Cloudflare Anycast
     └──────────────────────────────┘
```

**Что видит цензор / DPI:**

- TLS-соединение к IP-адресу Cloudflare (один из миллионов сайтов)
- SNI: `vpn.example.com` (ваш домен, выглядит легитимно)
- Весь payload зашифрован — WebSocket/gRPC внутри TLS

**Что НЕ может сделать цензор:**

- Определить что внутри TLS идёт VPN-трафик
- Узнать реальный IP вашего сервера
- Заблокировать конкретно ваш сервер (пришлось бы блокировать весь Cloudflare)

---

## Пошаговая инструкция

### Шаг 1. Регистрация в Cloudflare

1. Заходим на [dash.cloudflare.com](https://dash.cloudflare.com)
2. Регистрируем аккаунт (бесплатный план достаточен)
3. Нажимаем **"Add a Site"** → вводим ваш домен (например `example.com`)
4. Выбираем план **Free** → Continue

### Шаг 2. Перенос DNS на Cloudflare

1. Cloudflare покажет два nameserver'а, например:
   ```
   ada.ns.cloudflare.com
   bob.ns.cloudflare.com
   ```
2. Идём к регистратору домена (Namecheap, GoDaddy, REG.RU и т.д.)
3. Меняем NS-записи на указанные Cloudflare
4. Ждём до 24 часов (обычно 15-30 минут)
5. В Cloudflare проверяем: **Overview** → статус `Active`

### Шаг 3. Настройка DNS-записей

В Cloudflare Dashboard → **DNS** → **Records**:

| Type | Name  | Content          | Proxy                         | TTL  |
| ---- | ----- | ---------------- | ----------------------------- | ---- |
| A    | `vpn` | `YOUR_SERVER_IP` | ☁️ Proxied (оранжевое облако) | Auto |
| A    | `@`   | `YOUR_SERVER_IP` | ☁️ Proxied                    | Auto |

> **КРИТИЧНО:** Облако должно быть **оранжевым** (Proxied), НЕ серым (DNS only).
> Оранжевое облако = трафик идёт через CDN Cloudflare.
> Серое облако = DNS просто отдаёт ваш IP (нет маскировки).

### Шаг 4. Настройка SSL/TLS в Cloudflare

1. **SSL/TLS** → **Overview** → выбираем режим **Full (strict)**

   ```
   ❌ Off          — нет шифрования
   ❌ Flexible     — нет шифрования до сервера (MITM)
   ❌ Full         — самоподписанный серт на сервере
   ✅ Full (strict) — валидный серт на сервере (Let's Encrypt)
   ```

2. **SSL/TLS** → **Edge Certificates**:
   - Minimum TLS Version: **TLS 1.2**
   - TLS 1.3: **ON**
   - Always Use HTTPS: **ON**
   - Automatic HTTPS Rewrites: **ON**

### Шаг 5. Настройка WebSocket в Cloudflare

1. **Network** → включаем **WebSockets**: **ON**

   Это ОБЯЗАТЕЛЬНО для работы VLESS-WS и Trojan-WS через CDN.

2. **Network** → **gRPC**: **ON**

   Это нужно для VLESS-gRPC.

> **Примечание:** На бесплатном плане Cloudflare WebSocket и gRPC доступны.

### Шаг 6. Дополнительные настройки Cloudflare

#### Speed → Optimization

- **Auto Minify**: выключить всё (JS, CSS, HTML) — мы проксируем VPN, минификация не нужна
- **Brotli**: ON (сжатие обычных страниц)

#### Caching → Configuration

- **Browser Cache TTL**: Respect Existing Headers

#### Security → Settings

- **Security Level**: Essentially Off (или Low)

  > Если поставить Medium/High, Cloudflare может показывать капчу
  > VPN-клиентам и они не смогут подключиться.

- **Challenge Passage**: 30 minutes
- **Browser Integrity Check**: **OFF**

  > Если включено, VPN-клиенты (v2ray, Hiddify) будут заблокированы,
  > потому что они не браузеры.

#### Security → WAF

- Убедитесь, что правила WAF не блокируют WebSocket-подключения
- Если проблемы — создайте правило **Skip** для путей `/vless-ws*` и `/trojan-ws*`

#### Security → Bot Fight Mode

- **OFF** — иначе Cloudflare будет блокировать VPN-клиенты как ботов

### Шаг 7. Настройка клиента (v2rayNG / Hiddify / NekoBox)

#### VLESS + WebSocket + TLS (через Cloudflare CDN)

```
Протокол:     VLESS
Адрес:        vpn.example.com        ← ваш домен (НЕ IP сервера!)
Порт:         443
UUID:         <из Marzban панели>
Encryption:   none
Transport:    ws
Path:         /vless-ws
TLS:          tls
SNI:          vpn.example.com
Fingerprint:  chrome
AllowInsecure: false
```

#### VLESS + gRPC + TLS (через Cloudflare CDN)

```
Протокол:     VLESS
Адрес:        vpn.example.com
Порт:         443
UUID:         <из Marzban панели>
Encryption:   none
Transport:    grpc
ServiceName:  vless-grpc
TLS:          tls
SNI:          vpn.example.com
Fingerprint:  chrome
```

#### Trojan + WebSocket + TLS (через Cloudflare CDN)

```
Протокол:     Trojan
Адрес:        vpn.example.com
Порт:         443
Password:     <из Marzban панели>
Transport:    ws
Path:         /trojan-ws
TLS:          tls
SNI:          vpn.example.com
Fingerprint:  chrome
```

### Шаг 8. Оптимизация для обхода блокировок

#### 8.1. Использование кастомного CDN-IP (если домен заблокирован по SNI)

Если провайдер блочит по SNI (видит `vpn.example.com` в ClientHello):

1. Находим IP Cloudflare, который не заблокирован:

   ```bash
   # Список чистых IP Cloudflare:
   # https://www.cloudflare.com/ips/
   # Попробуйте разные IP из диапазонов
   nslookup vpn.example.com
   ```

2. В клиенте v2ray вместо домена ставим **IP Cloudflare**:
   ```
   Адрес:  104.16.xxx.xxx    ← IP Cloudflare
   SNI:    vpn.example.com    ← домен остаётся в SNI
   Host:   vpn.example.com    ← домен в HTTP Host header
   ```

#### 8.2. Если блокируют весь Cloudflare (экстремальный случай)

1. Используйте **REALITY протокол** — он не зависит от CDN:

   ```
   Протокол:     VLESS
   Адрес:        YOUR_SERVER_IP
   Порт:         2053
   Flow:         xtls-rprx-vision
   Security:     reality
   SNI:          www.google.com
   Fingerprint:  chrome
   Public Key:   <из scripts/05-setup-reality.sh>
   Short ID:     <из scripts/05-setup-reality.sh>
   ```

2. Или используйте **другой CDN-провайдер** (Gcore, Fastly, AWS CloudFront)

---

## Проверка работы

### На сервере:

```bash
# 1. Проверяем что домен резолвится в IP Cloudflare (не сервера!)
dig +short vpn.example.com
# Должен показать IP из диапазонов Cloudflare (104.x.x.x, 172.64.x.x)

# 2. Проверяем SSL
curl -I https://vpn.example.com
# Должен ответить 200 OK, заголовок cf-ray: xxxxx (Cloudflare)

# 3. Проверяем WebSocket endpoint
curl -i -N \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
  -H "Sec-WebSocket-Version: 13" \
  https://vpn.example.com/vless-ws
# Должен ответить 101 Switching Protocols

# 4. Проверяем что реальный IP скрыт
# На другом компьютере:
nslookup vpn.example.com
# НЕ должен показывать IP вашего сервера
```

### На клиенте:

1. Подключитесь через v2rayNG / Hiddify
2. Откройте [https://whatismyipaddress.com](https://whatismyipaddress.com)
3. IP должен быть **IP вашего VPN-сервера** (не Cloudflare и не ваш домашний)
4. Тест скорости: [https://speedtest.net](https://speedtest.net)

---

## Решение проблем

| Симптом                     | Причина                  | Решение                                                                |
| --------------------------- | ------------------------ | ---------------------------------------------------------------------- |
| 522 Connection timed out    | Сервер не отвечает       | Проверьте что nginx запущен и слушает 443                              |
| 521 Web server is down      | Nginx / приложение упало | `docker compose logs nginx`                                            |
| 525 SSL handshake failed    | Нет серта на сервере     | Получите Let's Encrypt: `docker compose run --rm certbot certonly ...` |
| 526 Invalid SSL certificate | Самоподписанный серт     | Смените режим SSL на "Full (strict)" + Let's Encrypt                   |
| 403 Forbidden на WS         | Cloudflare WAF блочит    | Отключите Bot Fight Mode, создайте Skip правило                        |
| Клиент не подключается      | WebSocket выключен       | Cloudflare → Network → WebSockets: ON                                  |
| Медленная скорость          | Бесплатный план CF       | Нормально: ~50-100 Мбит через CDN, CDN добавляет 10-30ms               |
| Error 1000 DNS              | Домен не привязан        | Проверьте A-запись в DNS Cloudflare                                    |
| Подключение отваливается    | Cloudflare таймаут 100с  | Для бесплатного плана лимит WebSocket 100с idle                        |

### Cloudflare WebSocket timeout (бесплатный план)

На бесплатном плане соединение WebSocket закрывается после **100 секунд** бездействия.
Для VPN это не проблема — клиент автоматически переподключается.

Если нужен длительный keepalive — можно настроить ping в xray:

```json
// В xray_config.json → inbound VLESS-WS → wsSettings:
"wsSettings": {
    "path": "/vless-ws",
    "headers": {},
    "heartbeatPeriod": 30
}
```

---

## Схема выбора протокола

```
Интернет работает нормально?
├── ДА → Используйте REALITY (порт 2053)
│         Максимальная скорость, лучшая маскировка
│
└── НЕТ, IP сервера заблокирован
    ├── Cloudflare доступен?
    │   ├── ДА → VLESS-WS через CDN (порт 443, path /vless-ws)
    │   │         Или VLESS-gRPC через CDN (порт 443, service vless-grpc)
    │   │
    │   └── НЕТ → Смените IP сервера или используйте другой CDN
    │
    └── Блокировка по SNI?
        ├── ДА → Используйте кастомный CDN-IP (шаг 8.1)
        └── НЕТ → Проверьте настройки клиента, порты, firewall
```
