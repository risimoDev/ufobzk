#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  VPNBZK — Интеллектуальный мастер-скрипт установки
#  Мягкая миграция с 3X-UI на VPNBZK (Marzban + FastAPI)
#
#  Использование:
#    curl -sL https://raw.githubusercontent.com/.../setup.sh | bash
#    # или локально:
#    bash setup.sh                    # интерактивный режим
#    bash setup.sh --auto             # автоматический (без подтверждений)
#    bash setup.sh --step 3           # с конкретного шага
#    bash setup.sh --skip-migration   # без миграции 3X-UI
#    bash setup.sh --dry-run          # только проверки, без изменений
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Цвета и символы ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
CHECK='✓'; CROSS='✗'; ARROW='→'; GEAR='⚙'

# ── Логирование ──
LOG_FILE="/tmp/vpnbzk-setup-$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1

info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[${CHECK}]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()    { echo -e "${RED}[${CROSS}]${NC}    $*"; }
step()    { echo -e "\n${BOLD}${CYAN}═══ $* ═══${NC}\n"; }
substep() { echo -e "  ${ARROW} $*"; }

die() {
    fail "$*"
    echo -e "\n${RED}Установка прервана. Лог: ${LOG_FILE}${NC}"
    exit 1
}

# ── Аргументы ──
AUTO_MODE=false
DRY_RUN=false
SKIP_MIGRATION=false
START_STEP=0
PROJECT_DIR="/opt/vpnbzk"
XUIDB_PATH="/etc/x-ui/x-ui.db"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --auto)           AUTO_MODE=true ;;
        --dry-run)        DRY_RUN=true ;;
        --skip-migration) SKIP_MIGRATION=true ;;
        --step)           START_STEP="$2"; shift ;;
        --dir)            PROJECT_DIR="$2"; shift ;;
        --xui-db)         XUIDB_PATH="$2"; shift ;;
        -h|--help)
            echo "Использование: bash setup.sh [--auto] [--dry-run] [--step N] [--skip-migration] [--dir /path] [--xui-db /path/to/x-ui.db]"
            exit 0 ;;
        *) warn "Неизвестный аргумент: $1" ;;
    esac
    shift
done

# ── Трекер состояния ──
STATE_FILE="/tmp/vpnbzk-setup-state"
TOTAL_STEPS=8

save_state() { echo "$1" > "$STATE_FILE"; }
load_state() { [[ -f "$STATE_FILE" ]] && cat "$STATE_FILE" || echo "0"; }

# Восстанавливаем состояние если есть
if [[ "$START_STEP" -eq 0 ]] && [[ -f "$STATE_FILE" ]]; then
    SAVED=$(load_state)
    if [[ "$SAVED" -gt 0 ]] && [[ "$SAVED" -lt "$TOTAL_STEPS" ]]; then
        if [[ "$AUTO_MODE" == false ]]; then
            echo -e "${YELLOW}Обнаружена незавершённая установка (шаг ${SAVED}/${TOTAL_STEPS}).${NC}"
            read -rp "Продолжить с шага $((SAVED + 1))? [Y/n] " RESUME
            if [[ "${RESUME,,}" != "n" ]]; then
                START_STEP=$((SAVED + 1))
                info "Продолжаем с шага $START_STEP"
            fi
        else
            START_STEP=$((SAVED + 1))
            info "Авто-режим: продолжаем с шага $START_STEP"
        fi
    fi
fi

should_run() { [[ "$1" -ge "$START_STEP" ]]; }

confirm() {
    if [[ "$AUTO_MODE" == true ]]; then return 0; fi
    read -rp "$1 [Y/n] " ANS
    [[ "${ANS,,}" != "n" ]]
}

# ── Retry с exponential backoff ──
retry() {
    local max_attempts=$1; shift
    local delay=2
    local attempt=1
    while [[ $attempt -le $max_attempts ]]; do
        if "$@"; then return 0; fi
        if [[ $attempt -lt $max_attempts ]]; then
            warn "Попытка $attempt/$max_attempts не удалась, повтор через ${delay}с..."
            sleep "$delay"
            delay=$((delay * 2))
        fi
        attempt=$((attempt + 1))
    done
    return 1
}

# ═══════════════════════════════════════════════════════════════
#  БАННЕР
# ═══════════════════════════════════════════════════════════════

echo -e "${BOLD}${CYAN}"
cat << 'BANNER'
  ╔══════════════════════════════════════════════════╗
  ║           🛸 VPNBZK — НИИ АЯ Setup             ║
  ║                                                  ║
  ║   Мягкая миграция 3X-UI → Marzban + FastAPI     ║
  ║                                                  ║
  ║   Шаги:                                          ║
  ║   1. Preflight-проверки                          ║
  ║   2. Подготовка сервера                          ║
  ║   3. Бэкап 3X-UI                                ║
  ║   4. Установка проекта + .env                    ║
  ║   5. SSL-сертификат                              ║
  ║   6. Запуск Docker-стека                         ║
  ║   7. Миграция пользователей 3X-UI → Marzban     ║
  ║   8. Настройка REALITY + (опц.) WARP            ║
  ╚══════════════════════════════════════════════════╝
BANNER
echo -e "${NC}"

info "Лог: $LOG_FILE"
info "Режим: $(if $AUTO_MODE; then echo 'авто'; else echo 'интерактивный'; fi)$(if $DRY_RUN; then echo ' (dry-run)'; fi)"
echo ""

# ═══════════════════════════════════════════════════════════════
#  ШАГ 1: PREFLIGHT — проверки перед установкой
# ═══════════════════════════════════════════════════════════════

if should_run 1; then
    step "Шаг 1/$TOTAL_STEPS: Preflight-проверки"

    # Root
    if [[ "$(id -u)" -ne 0 ]]; then
        die "Запустите от root: sudo bash setup.sh"
    fi
    ok "Root-доступ"

    # ОС
    if grep -qiE 'ubuntu|debian' /etc/os-release 2>/dev/null; then
        OS_NAME=$(grep ^PRETTY_NAME /etc/os-release | cut -d= -f2 | tr -d '"')
        ok "ОС: $OS_NAME"
    else
        die "Поддерживаются только Ubuntu/Debian"
    fi

    # RAM
    TOTAL_RAM_MB=$(free -m | awk '/^Mem:/ {print $2}')
    if [[ $TOTAL_RAM_MB -lt 512 ]]; then
        die "Минимум 512 МБ RAM (сейчас: ${TOTAL_RAM_MB} МБ)"
    fi
    ok "RAM: ${TOTAL_RAM_MB} МБ"

    # Диск
    FREE_DISK_GB=$(df / --output=avail -BG | tail -1 | tr -d ' G')
    if [[ $FREE_DISK_GB -lt 3 ]]; then
        die "Минимум 3 ГБ свободного места (сейчас: ${FREE_DISK_GB} ГБ)"
    fi
    ok "Диск: ${FREE_DISK_GB} ГБ свободно"

    # Порты
    PORTS_NEEDED=(80 443)
    for PORT in "${PORTS_NEEDED[@]}"; do
        if ss -tlnp | grep -q ":${PORT} " 2>/dev/null; then
            # Определяем что слушает порт
            LISTENER=$(ss -tlnp | grep ":${PORT} " | awk '{print $NF}' | head -1)
            if echo "$LISTENER" | grep -qi "x-ui\|3x-ui\|xray"; then
                warn "Порт $PORT занят 3X-UI — будет остановлен при миграции"
            elif echo "$LISTENER" | grep -qi "nginx\|docker"; then
                warn "Порт $PORT занят ($LISTENER) — разберёмся позже"
            else
                warn "Порт $PORT занят: $LISTENER"
            fi
        else
            ok "Порт $PORT свободен"
        fi
    done

    # Интернет
    if retry 3 curl -sf --max-time 5 https://api.ipify.org > /dev/null 2>&1; then
        SERVER_IP=$(curl -sf --max-time 5 https://api.ipify.org)
        ok "Интернет: OK (IP: $SERVER_IP)"
    else
        die "Нет доступа к интернету"
    fi

    # Docker (может не быть — установим)
    if command -v docker &>/dev/null; then
        DOCKER_VER=$(docker --version | awk '{print $3}' | tr -d ',')
        ok "Docker: $DOCKER_VER (установлен)"
        HAS_DOCKER=true
    else
        warn "Docker не установлен — установим на шаге 2"
        HAS_DOCKER=false
    fi

    # 3X-UI
    HAS_3XUI=false
    if [[ -f "$XUIDB_PATH" ]]; then
        XUIDB_SIZE=$(stat -c%s "$XUIDB_PATH" 2>/dev/null || echo "0")
        CLIENT_COUNT=$(sqlite3 "$XUIDB_PATH" "SELECT COUNT(*) FROM inbounds;" 2>/dev/null || echo "?")
        ok "3X-UI найден: $XUIDB_PATH (${XUIDB_SIZE} байт, inbounds: $CLIENT_COUNT)"
        HAS_3XUI=true
    elif systemctl is-active --quiet x-ui 2>/dev/null; then
        warn "3X-UI запущен, но БД не найдена по пути $XUIDB_PATH"
        warn "Укажите путь через --xui-db /path/to/x-ui.db"
    else
        info "3X-UI не обнаружен — миграция пользователей будет пропущена"
    fi

    # Домен (если уже настроен)
    if [[ -f "$PROJECT_DIR/.env" ]]; then
        EXISTING_DOMAIN=$(grep '^DOMAIN=' "$PROJECT_DIR/.env" 2>/dev/null | cut -d= -f2)
        if [[ -n "$EXISTING_DOMAIN" ]] && [[ "$EXISTING_DOMAIN" != "vpn.example.com" ]]; then
            ok "Найдена конфигурация: $EXISTING_DOMAIN"
        fi
    fi

    save_state 1
    ok "Все preflight-проверки пройдены"
fi

# ═══════════════════════════════════════════════════════════════
#  ШАГ 2: ПОДГОТОВКА СЕРВЕРА
# ═══════════════════════════════════════════════════════════════

if should_run 2; then
    step "Шаг 2/$TOTAL_STEPS: Подготовка сервера"

    if [[ "$DRY_RUN" == true ]]; then
        info "[dry-run] Пропуск установки пакетов"
    else
        # Обновление системы
        substep "Обновление пакетов..."
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get upgrade -y -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold"
        ok "Система обновлена"

        # Установка базовых утилит
        substep "Установка утилит..."
        apt-get install -y -qq \
            ca-certificates curl git wget htop jq sqlite3 \
            fail2ban ufw unattended-upgrades \
            > /dev/null 2>&1
        ok "Утилиты установлены"

        # Docker
        if [[ "${HAS_DOCKER:-false}" == false ]]; then
            substep "Установка Docker..."
            install -m 0755 -d /etc/apt/keyrings
            curl -fsSL https://download.docker.com/linux/$(. /etc/os-release && echo "$ID")/gpg \
                | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
            chmod a+r /etc/apt/keyrings/docker.gpg

            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
                https://download.docker.com/linux/$(. /etc/os-release && echo "$ID") \
                $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
                tee /etc/apt/sources.list.d/docker.list > /dev/null

            apt-get update -qq
            apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
                docker-buildx-plugin docker-compose-plugin > /dev/null 2>&1
            systemctl enable docker --now
            ok "Docker установлен: $(docker --version | awk '{print $3}')"
        else
            ok "Docker уже установлен — пропуск"
        fi

        # Firewall (не трогаем если уже настроен)
        if ufw status | grep -q "inactive"; then
            substep "Настройка файрвола..."
            ufw default deny incoming
            ufw default allow outgoing
            ufw allow ssh
            ufw allow 80/tcp
            ufw allow 443/tcp
            ufw allow 2053/tcp comment "REALITY VPN"
            ufw --force enable
            ok "Файрвол включён"
        else
            # Только добавляем нужные порты, не сбрасывая
            substep "Файрвол уже активен — добавляем порты..."
            ufw allow 80/tcp 2>/dev/null || true
            ufw allow 443/tcp 2>/dev/null || true
            ufw allow 2053/tcp comment "REALITY VPN" 2>/dev/null || true
            ok "Порты добавлены в файрвол"
        fi

        # Fail2ban
        substep "Настройка Fail2ban..."
        systemctl enable fail2ban --now 2>/dev/null || true

        cat > /etc/fail2ban/jail.local <<'EOF'
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port    = ssh
logpath = %(sshd_log)s
backend = systemd
EOF
        # Копируем фильтры VPNBZK если есть
        if [[ -d "$PROJECT_DIR/fail2ban" ]]; then
            cp "$PROJECT_DIR/fail2ban/filter.d/"*.conf /etc/fail2ban/filter.d/ 2>/dev/null || true
            cp "$PROJECT_DIR/fail2ban/jail.d/"*.conf /etc/fail2ban/jail.d/ 2>/dev/null || true
        fi
        systemctl restart fail2ban 2>/dev/null || true
        ok "Fail2ban настроен"

        # Swap
        if [[ ! -f /swapfile ]] && [[ $TOTAL_RAM_MB -lt 2048 ]]; then
            substep "Создание swap (2 ГБ)..."
            dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
            chmod 600 /swapfile
            mkswap /swapfile > /dev/null
            swapon /swapfile
            grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
            ok "Swap создан"
        fi

        # Sysctl оптимизации
        cat > /etc/sysctl.d/99-vpnbzk.conf <<'EOF'
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
net.ipv4.ip_forward = 1
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
fs.file-max = 1048576
EOF
        sysctl --system > /dev/null 2>&1
        ok "Системные оптимизации применены"
    fi

    save_state 2
fi

# ═══════════════════════════════════════════════════════════════
#  ШАГ 3: БЭКАП 3X-UI (мягкая подготовка к замене)
# ═══════════════════════════════════════════════════════════════

if should_run 3; then
    step "Шаг 3/$TOTAL_STEPS: Бэкап 3X-UI"

    BACKUP_DIR="$PROJECT_DIR/backups/3xui_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$BACKUP_DIR"

    if [[ "${HAS_3XUI:-false}" == true ]]; then
        substep "Создание бэкапа БД 3X-UI..."
        cp "$XUIDB_PATH" "$BACKUP_DIR/x-ui.db"
        ok "БД скопирована: $BACKUP_DIR/x-ui.db"

        # Бэкап конфигов 3X-UI
        for CONF in /etc/x-ui/config.json /usr/local/x-ui/config.json /usr/local/x-ui/bin/config.json; do
            if [[ -f "$CONF" ]]; then
                cp "$CONF" "$BACKUP_DIR/" 2>/dev/null || true
                ok "Конфиг скопирован: $(basename $CONF)"
            fi
        done

        # Бэкап сертификатов (если есть)
        for CERT_DIR in /root/cert /etc/letsencrypt; do
            if [[ -d "$CERT_DIR" ]]; then
                cp -r "$CERT_DIR" "$BACKUP_DIR/" 2>/dev/null || true
                ok "Сертификаты скопированы: $CERT_DIR"
            fi
        done

        # Подсчитаем пользователей для миграции
        if command -v sqlite3 &>/dev/null; then
            USER_COUNT=0
            INBOUNDS=$(sqlite3 "$BACKUP_DIR/x-ui.db" "SELECT settings FROM inbounds;" 2>/dev/null || echo "")
            if [[ -n "$INBOUNDS" ]]; then
                USER_COUNT=$(echo "$INBOUNDS" | python3 -c "
import sys, json
total = 0
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        total += len(d.get('clients', []))
    except: pass
print(total)
" 2>/dev/null || echo "?")
            fi
            ok "Найдено пользователей для миграции: $USER_COUNT"
        fi

        info "Бэкап 3X-UI сохранён в: $BACKUP_DIR"
    else
        info "3X-UI не обнаружен — бэкап пропущен"
    fi

    save_state 3
fi

# ═══════════════════════════════════════════════════════════════
#  ШАГ 4: УСТАНОВКА ПРОЕКТА + КОНФИГУРАЦИЯ .env
# ═══════════════════════════════════════════════════════════════

if should_run 4; then
    step "Шаг 4/$TOTAL_STEPS: Установка проекта"

    mkdir -p "$PROJECT_DIR"

    # Определяем откуда копируем (локальная директория или git)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    SOURCE_DIR="$(dirname "$SCRIPT_DIR")"

    if [[ -f "$SOURCE_DIR/docker-compose.yml" ]] && [[ -d "$SOURCE_DIR/app" ]]; then
        substep "Копирование из локальной директории: $SOURCE_DIR"
        rsync -a --exclude='.git' --exclude='data/' --exclude='.env' \
              --exclude='__pycache__' --exclude='*.pyc' --exclude='.warp' \
              "$SOURCE_DIR/" "$PROJECT_DIR/"
        ok "Файлы скопированы"
    elif [[ -f "$PROJECT_DIR/docker-compose.yml" ]]; then
        ok "Проект уже установлен в $PROJECT_DIR"
    else
        die "Не найдены файлы проекта. Сначала склонируйте репозиторий или скопируйте файлы в $PROJECT_DIR"
    fi

    # ── Конфигурация .env ──

    ENV_FILE="$PROJECT_DIR/.env"
    ENV_EXAMPLE="$PROJECT_DIR/.env.example"

    if [[ -f "$ENV_FILE" ]]; then
        CURRENT_DOMAIN=$(grep '^DOMAIN=' "$ENV_FILE" | cut -d= -f2)
        if [[ "$CURRENT_DOMAIN" != "vpn.example.com" ]] && [[ -n "$CURRENT_DOMAIN" ]]; then
            ok ".env уже настроен (домен: $CURRENT_DOMAIN)"
            if ! confirm "Перенастроить .env?"; then
                save_state 4
                # Перескакиваем к SSL
                ok "Используем текущий .env"
                # Загружаем переменные
                set -a; source "$ENV_FILE"; set +a
                info "Шаг 4 — готово (существующая конфигурация)"
                save_state 4
                # Переходим к шагу 5 в основном потоке
                SKIP_ENV_SETUP=true
            fi
        fi
    fi

    if [[ "${SKIP_ENV_SETUP:-false}" != true ]]; then
        substep "Настройка .env..."

        # Определяем IP сервера
        SERVER_IP="${SERVER_IP:-$(curl -sf --max-time 5 https://api.ipify.org 2>/dev/null || echo '')}"

        if [[ "$AUTO_MODE" == true ]]; then
            # В авто-режиме берём из текущего .env или дефолты
            if [[ -f "$ENV_FILE" ]]; then
                set -a; source "$ENV_FILE"; set +a
            fi
            DOMAIN="${DOMAIN:-vpn.example.com}"
            EMAIL="${EMAIL:-admin@example.com}"
            TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
            TELEGRAM_BOT_USERNAME="${TELEGRAM_BOT_USERNAME:-}"
            MARZBAN_ADMIN_PASS="${MARZBAN_ADMIN_PASS:-}"
            ADMIN_IP="${MARZBAN_WHITELIST_IP:-$SERVER_IP}"
            SUPERADMIN_TG_ID="${SUPERADMIN_TELEGRAM_ID:-}"
        else
            # Интерактивный ввод с подсказками
            echo ""
            echo -e "${BOLD}Введите данные для настройки:${NC}"
            echo ""

            # Домен
            DEFAULT_DOMAIN=$(grep '^DOMAIN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
            [[ "$DEFAULT_DOMAIN" == "vpn.example.com" ]] && DEFAULT_DOMAIN=""
            read -rp "  Домен (например vpn.mysite.com): ${DEFAULT_DOMAIN:+[$DEFAULT_DOMAIN] }" DOMAIN
            DOMAIN="${DOMAIN:-$DEFAULT_DOMAIN}"
            [[ -z "$DOMAIN" ]] && die "Домен обязателен"

            # Проверяем DNS
            substep "Проверка DNS для $DOMAIN..."
            RESOLVED_IP=$(dig +short "$DOMAIN" 2>/dev/null | head -1)
            if [[ -n "$RESOLVED_IP" ]]; then
                if [[ "$RESOLVED_IP" == "$SERVER_IP" ]]; then
                    ok "DNS настроен корректно: $DOMAIN → $SERVER_IP"
                else
                    warn "DNS: $DOMAIN → $RESOLVED_IP (IP сервера: $SERVER_IP)"
                    warn "Если используете Cloudflare CDN — это нормально"
                fi
            else
                warn "DNS для $DOMAIN не резолвится"
                warn "Настройте A-запись: $DOMAIN → $SERVER_IP"
                if ! confirm "Продолжить без DNS?"; then
                    die "Настройте DNS и перезапустите скрипт"
                fi
            fi

            # Email
            DEFAULT_EMAIL=$(grep '^EMAIL=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
            read -rp "  Email (для SSL): ${DEFAULT_EMAIL:+[$DEFAULT_EMAIL] }" EMAIL
            EMAIL="${EMAIL:-$DEFAULT_EMAIL}"

            # Telegram Bot
            DEFAULT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
            if [[ -n "$DEFAULT_TOKEN" ]] && [[ "$DEFAULT_TOKEN" != "your-bot-token" ]]; then
                read -rp "  Telegram Bot Token [сохранить текущий]: " TELEGRAM_BOT_TOKEN
                TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$DEFAULT_TOKEN}"
            else
                read -rp "  Telegram Bot Token: " TELEGRAM_BOT_TOKEN
            fi

            DEFAULT_USERNAME=$(grep '^TELEGRAM_BOT_USERNAME=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
            read -rp "  Telegram Bot Username (без @): ${DEFAULT_USERNAME:+[$DEFAULT_USERNAME] }" TELEGRAM_BOT_USERNAME
            TELEGRAM_BOT_USERNAME="${TELEGRAM_BOT_USERNAME:-$DEFAULT_USERNAME}"

            # Суперадмин
            DEFAULT_SA=$(grep '^SUPERADMIN_TELEGRAM_ID=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
            read -rp "  Telegram ID суперадмина: ${DEFAULT_SA:+[$DEFAULT_SA] }" SUPERADMIN_TG_ID
            SUPERADMIN_TG_ID="${SUPERADMIN_TG_ID:-$DEFAULT_SA}"
            [[ -z "$SUPERADMIN_TG_ID" ]] && die "SUPERADMIN_TELEGRAM_ID обязателен"

            # Пароль Marzban
            DEFAULT_PASS=$(grep '^MARZBAN_ADMIN_PASS=' "$ENV_FILE" 2>/dev/null | cut -d= -f2)
            if [[ -n "$DEFAULT_PASS" ]] && [[ "$DEFAULT_PASS" != "changeme" ]]; then
                read -rp "  Marzban пароль [сохранить текущий]: " MARZBAN_ADMIN_PASS
                MARZBAN_ADMIN_PASS="${MARZBAN_ADMIN_PASS:-$DEFAULT_PASS}"
            else
                MARZBAN_ADMIN_PASS=$(openssl rand -base64 18 | tr -dc 'a-zA-Z0-9' | head -c16)
                info "Сгенерирован пароль Marzban: $MARZBAN_ADMIN_PASS"
            fi

            # IP для админки
            read -rp "  IP для доступа к админке [${SERVER_IP}]: " ADMIN_IP
            ADMIN_IP="${ADMIN_IP:-$SERVER_IP}"
            echo ""
        fi

        # Генерация SECRET_KEY
        SECRET_KEY=$(openssl rand -hex 32)

        # Записываем .env
        if [[ -f "$ENV_FILE" ]]; then
            cp "$ENV_FILE" "${ENV_FILE}.bak"
            info "Бэкап предыдущего .env: ${ENV_FILE}.bak"
        fi

        cat > "$ENV_FILE" <<ENVEOF
# ═══════════════════════════════════════════
#  VPNBZK — Production Environment
#  Сгенерировано: $(date '+%Y-%m-%d %H:%M:%S')
# ═══════════════════════════════════════════

# ── Домен и SSL ──
DOMAIN=${DOMAIN}
EMAIL=${EMAIL}

# ── Секреты приложения ──
SECRET_KEY=${SECRET_KEY}

# ── Telegram Bot ──
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
TELEGRAM_BOT_USERNAME=${TELEGRAM_BOT_USERNAME}
WEBAPP_URL=https://${DOMAIN}

# ── Marzban API (внутренняя сеть Docker) ──
MARZBAN_BASE_URL=http://marzban:8000
MARZBAN_ADMIN_USER=admin
MARZBAN_ADMIN_PASS=${MARZBAN_ADMIN_PASS}

# ── Marzban Panel ──
SUDO_USERNAME=admin
SUDO_PASSWORD=${MARZBAN_ADMIN_PASS}

# ── Доступ ──
ADMIN_IPS=${ADMIN_IP:-127.0.0.1}
MARZBAN_WHITELIST_IP=${ADMIN_IP:-127.0.0.1}
SUPERADMIN_TELEGRAM_ID=${SUPERADMIN_TG_ID}

# ── Xray (Marzban) ──
XRAY_FALLBACK_PORT=8443

# ── REALITY ──
REALITY_PORT=2053
REALITY_DEST=www.google.com:443
REALITY_SERVER_NAMES=www.google.com,google.com
REALITY_PRIVATE_KEY=
REALITY_PUBLIC_KEY=

# ── WARP (опционально) ──
WARP_PRIVATE_KEY=
WARP_RESERVED=0,0,0
ENVEOF
        chmod 600 "$ENV_FILE"
        ok ".env создан (SECRET_KEY сгенерирован, chmod 600)"
    fi

    # Создаём директорию данных
    mkdir -p "$PROJECT_DIR/data"

    save_state 4
fi

# ═══════════════════════════════════════════════════════════════
#  ШАГ 5: SSL-СЕРТИФИКАТ
# ═══════════════════════════════════════════════════════════════

if should_run 5; then
    step "Шаг 5/$TOTAL_STEPS: SSL-сертификат"

    cd "$PROJECT_DIR"
    set -a; source .env; set +a

    # Проверяем есть ли уже сертификат
    CERT_EXISTS=false
    if docker volume ls --format '{{.Name}}' | grep -q 'certbot-certs' 2>/dev/null; then
        # Проверяем внутри volume
        if docker run --rm -v certbot-certs:/certs alpine test -f "/certs/live/${DOMAIN}/fullchain.pem" 2>/dev/null; then
            CERT_EXISTS=true
        fi
    fi

    if [[ "$CERT_EXISTS" == true ]]; then
        ok "SSL-сертификат уже существует для $DOMAIN"
    elif [[ "$DRY_RUN" == true ]]; then
        info "[dry-run] Пропуск получения SSL"
    else
        substep "Получение SSL-сертификата для $DOMAIN..."

        # Проверяем что порты 80/443 свободны (останавливаем временно 3X-UI если нужно)
        XUI_WAS_RUNNING=false
        if systemctl is-active --quiet x-ui 2>/dev/null; then
            warn "Временная остановка 3X-UI для получения сертификата..."
            systemctl stop x-ui
            XUI_WAS_RUNNING=true
            sleep 2
        fi

        # Также проверяем Docker nginx
        if docker ps --format '{{.Names}}' | grep -q 'vpnbzk-nginx' 2>/dev/null; then
            docker compose stop nginx 2>/dev/null || true
            sleep 1
        fi

        # Создаём минимальный nginx для ACME challenge
        NGINX_INITIAL="$PROJECT_DIR/nginx/nginx-initial.conf"
        cat > "$NGINX_INITIAL" <<'TMPNGINX'
events { worker_connections 256; }
http {
    server {
        listen 80;
        server_name _;
        location /.well-known/acme-challenge/ { root /var/www/certbot; }
        location / { return 444; }
    }
}
TMPNGINX

        # Запускаем временный nginx
        docker run -d --name vpnbzk-certbot-nginx --rm \
            -p 80:80 \
            -v "$NGINX_INITIAL:/etc/nginx/nginx.conf:ro" \
            -v vpnbzk_certbot-webroot:/var/www/certbot \
            nginx:1.27-alpine 2>/dev/null || true

        sleep 2

        # Получаем сертификат
        if retry 2 docker run --rm \
            -v vpnbzk_certbot-certs:/etc/letsencrypt \
            -v vpnbzk_certbot-webroot:/var/www/certbot \
            certbot/certbot certonly \
            --webroot -w /var/www/certbot \
            -d "$DOMAIN" \
            --email "$EMAIL" \
            --agree-tos --no-eff-email --non-interactive; then
            ok "SSL-сертификат получен для $DOMAIN"
        else
            warn "Не удалось получить SSL. Проверьте что DNS настроен: $DOMAIN → $SERVER_IP"
            warn "Можно продолжить и получить сертификат позже"
        fi

        # Останавливаем временный nginx
        docker stop vpnbzk-certbot-nginx 2>/dev/null || true
        rm -f "$NGINX_INITIAL"

        # Возвращаем 3X-UI если был запущен (пока ещё нужен пользователям)
        if [[ "$XUI_WAS_RUNNING" == true ]]; then
            substep "Возвращаем 3X-UI (пока идёт настройка)..."
            systemctl start x-ui
            ok "3X-UI запущен обратно (будет остановлен на шаге 6)"
        fi
    fi

    save_state 5
fi

# ═══════════════════════════════════════════════════════════════
#  ШАГ 6: ЗАПУСК DOCKER-СТЕКА
# ═══════════════════════════════════════════════════════════════

if should_run 6; then
    step "Шаг 6/$TOTAL_STEPS: Запуск Docker-стека"

    cd "$PROJECT_DIR"
    set -a; source .env; set +a

    if [[ "$DRY_RUN" == true ]]; then
        info "[dry-run] Пропуск запуска Docker"
    else
        # ── Мягкая остановка 3X-UI ──
        if systemctl is-active --quiet x-ui 2>/dev/null; then
            echo ""
            warn "3X-UI сейчас запущен и обслуживает пользователей."
            warn "После остановки 3X-UI и запуска Marzban:"
            warn "  • Пользователи с мигрированными UUID продолжат работать"
            warn "  • Новые подключения пойдут через Marzban"
            echo ""

            if confirm "Остановить 3X-UI и запустить VPNBZK?"; then
                substep "Остановка 3X-UI..."
                systemctl stop x-ui
                systemctl disable x-ui 2>/dev/null || true
                ok "3X-UI остановлен и отключён"

                # Ждём освобождения портов
                sleep 3
                for PORT in 80 443; do
                    if ss -tlnp | grep -q ":${PORT} "; then
                        warn "Порт $PORT всё ещё занят, ждём..."
                        sleep 5
                    fi
                done
            else
                warn "Пропускаем запуск Docker — 3X-UI продолжает работать"
                warn "Запустите позже: cd $PROJECT_DIR && systemctl stop x-ui && docker compose up -d"
                save_state 6
                exit 0
            fi
        fi

        # ── Сборка и запуск ──
        substep "Сборка Docker-образов..."
        docker compose build --quiet 2>&1 | tail -3
        ok "Образы собраны"

        substep "Запуск стека..."
        docker compose up -d
        ok "Контейнеры запущены"

        # ── Ожидание готовности ──
        substep "Ожидание готовности сервисов..."
        MAX_WAIT=120
        WAITED=0
        ALL_HEALTHY=false

        while [[ $WAITED -lt $MAX_WAIT ]]; do
            HEALTH=$(docker compose ps --format json 2>/dev/null | python3 -c "
import sys, json
services = {}
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        svc = json.loads(line)
        name = svc.get('Service', svc.get('Name', ''))
        health = svc.get('Health', svc.get('State', ''))
        services[name] = health
    except: pass
healthy = sum(1 for h in services.values() if h in ('healthy', 'running'))
total = len(services)
print(f'{healthy}/{total}')
for k, v in services.items():
    print(f'  {k}: {v}')
" 2>/dev/null || echo "0/0")

            READY=$(echo "$HEALTH" | head -1)
            READY_COUNT=$(echo "$READY" | cut -d/ -f1)
            TOTAL_COUNT=$(echo "$READY" | cut -d/ -f2)

            if [[ "$READY_COUNT" -ge 3 ]] && [[ "$TOTAL_COUNT" -ge 3 ]]; then
                ALL_HEALTHY=true
                break
            fi

            printf "\r  Сервисы: %s (ожидание %ds/%ds)  " "$READY" "$WAITED" "$MAX_WAIT"
            sleep 5
            WAITED=$((WAITED + 5))
        done
        echo ""

        if [[ "$ALL_HEALTHY" == true ]]; then
            ok "Все сервисы запущены и здоровы"
            docker compose ps --format "table {{.Name}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null
        else
            warn "Не все сервисы стали healthy за ${MAX_WAIT}с"
            docker compose ps
            warn "Проверьте логи: docker compose logs -f"
        fi

        # ── Cron для автообновления SSL ──
        CRON_LINE="0 3 * * 0 cd $PROJECT_DIR && docker compose run --rm certbot renew --quiet && docker compose exec nginx nginx -s reload"
        if ! crontab -l 2>/dev/null | grep -qF "certbot renew"; then
            (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
            ok "Cron: автообновление SSL каждое воскресенье в 03:00"
        fi

        # ── Logrotate ──
        cat > /etc/logrotate.d/vpnbzk <<LOGEOF
/var/lib/docker/containers/*/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    maxsize 50M
    copytruncate
}
LOGEOF
        ok "Logrotate настроен"
    fi

    save_state 6
fi

# ═══════════════════════════════════════════════════════════════
#  ШАГ 7: МИГРАЦИЯ ПОЛЬЗОВАТЕЛЕЙ 3X-UI → MARZBAN
# ═══════════════════════════════════════════════════════════════

if should_run 7; then
    step "Шаг 7/$TOTAL_STEPS: Миграция пользователей"

    cd "$PROJECT_DIR"
    set -a; source .env; set +a

    if [[ "$SKIP_MIGRATION" == true ]]; then
        info "Миграция пропущена (--skip-migration)"
    elif [[ "${HAS_3XUI:-false}" == false ]]; then
        info "3X-UI не обнаружен — миграция пропущена"
    elif [[ "$DRY_RUN" == true ]]; then
        info "[dry-run] Миграция будет пропущена"
    else
        # Находим бэкап БД
        MIGRATION_DB=""
        BACKUP_DIR=$(ls -dt "$PROJECT_DIR/backups/3xui_"* 2>/dev/null | head -1)
        if [[ -n "$BACKUP_DIR" ]] && [[ -f "$BACKUP_DIR/x-ui.db" ]]; then
            MIGRATION_DB="$BACKUP_DIR/x-ui.db"
        elif [[ -f "$XUIDB_PATH" ]]; then
            MIGRATION_DB="$XUIDB_PATH"
        fi

        if [[ -z "$MIGRATION_DB" ]]; then
            warn "БД 3X-UI не найдена — миграция пропущена"
        else
            substep "Миграция из: $MIGRATION_DB"

            # Ждём готовности Marzban API
            substep "Ожидание готовности Marzban API..."
            MARZBAN_READY=false
            for i in $(seq 1 30); do
                if curl -sf --max-time 3 http://localhost:8000/api/admin/token > /dev/null 2>&1; then
                    MARZBAN_READY=true
                    break
                fi
                # Пробуем через Docker network
                MARZBAN_PORT=$(docker port vpnbzk-marzban 8000 2>/dev/null | head -1 | cut -d: -f2)
                if [[ -n "$MARZBAN_PORT" ]] && curl -sf --max-time 3 "http://localhost:${MARZBAN_PORT}/api/admin/token" > /dev/null 2>&1; then
                    MARZBAN_READY=true
                    break
                fi
                sleep 2
            done

            if [[ "$MARZBAN_READY" == false ]]; then
                warn "Marzban API не отвечает — миграция отложена"
                warn "Запустите позже: bash scripts/04-migrate-3xui.sh $MIGRATION_DB"
            else
                # Получаем токен Marzban
                MARZBAN_URL="http://localhost:${MARZBAN_PORT:-8000}"
                TOKEN_RESP=$(curl -sf --max-time 10 -X POST "$MARZBAN_URL/api/admin/token" \
                    -H "Content-Type: application/x-www-form-urlencoded" \
                    -d "username=${MARZBAN_ADMIN_USER:-admin}&password=${MARZBAN_ADMIN_PASS}" 2>/dev/null || echo "")

                MARZBAN_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || echo "")

                if [[ -z "$MARZBAN_TOKEN" ]]; then
                    warn "Не удалось авторизоваться в Marzban"
                    warn "Запустите миграцию вручную: bash scripts/04-migrate-3xui.sh"
                else
                    ok "Marzban API подключён"

                    # Извлекаем пользователей из 3X-UI
                    MIGRATION_LOG="$BACKUP_DIR/migration.log"
                    MIGRATED=0; SKIPPED=0; FAILED=0

                    # Парсим клиентов
                    CLIENTS_JSON=$(sqlite3 "$MIGRATION_DB" "SELECT settings FROM inbounds;" 2>/dev/null | python3 -c "
import sys, json
clients = []
for line in sys.stdin:
    line = line.strip()
    if not line: continue
    try:
        d = json.loads(line)
        for c in d.get('clients', []):
            clients.append(c)
    except: pass
json.dump(clients, sys.stdout)
" 2>/dev/null || echo "[]")

                    TOTAL_CLIENTS=$(echo "$CLIENTS_JSON" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")
                    info "Найдено клиентов: $TOTAL_CLIENTS"

                    if [[ "$TOTAL_CLIENTS" -gt 0 ]]; then
                        # Мигрируем через Python для надёжности
                        python3 - "$MARZBAN_URL" "$MARZBAN_TOKEN" "$CLIENTS_JSON" "$MIGRATION_LOG" <<'MIGRATE_PY'
import sys, json, urllib.request, urllib.error, time

marzban_url = sys.argv[1]
token = sys.argv[2]
clients = json.loads(sys.argv[3])
log_path = sys.argv[4]

migrated = 0; skipped = 0; failed = 0

with open(log_path, 'w') as log:
    for client in clients:
        email = client.get('email', '') or client.get('id', '')[:8]
        uuid_val = client.get('id', '')
        flow = client.get('flow', '')
        total_gb = client.get('totalGB', 0)
        expiry_time = client.get('expiryTime', 0)

        username = email.replace('@', '_').replace(' ', '_')[:32] or f"user_{uuid_val[:8]}"

        # Подготовка payload
        payload = {
            "username": username,
            "proxies": {
                "vless": {"id": uuid_val, "flow": flow} if uuid_val else {},
            },
            "inbounds": {},
            "data_limit": int(total_gb * 1073741824) if total_gb else 0,
            "status": "active",
        }

        if expiry_time and expiry_time > 0:
            # 3X-UI хранит в миллисекундах
            payload["expire"] = int(expiry_time / 1000) if expiry_time > 1e12 else int(expiry_time)

        try:
            req = urllib.request.Request(
                f"{marzban_url}/api/user",
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                migrated += 1
                log.write(f"OK  {username} (uuid={uuid_val[:8]}...)\n")
                print(f"  ✓ {username}")
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if "already exists" in body.lower() or e.code == 409:
                skipped += 1
                log.write(f"SKIP {username} (already exists)\n")
            else:
                failed += 1
                log.write(f"FAIL {username}: {e.code} {body[:200]}\n")
                print(f"  ✗ {username}: {e.code}")
        except Exception as e:
            failed += 1
            log.write(f"FAIL {username}: {e}\n")

        time.sleep(0.2)

    log.write(f"\n--- ИТОГО: migrated={migrated} skipped={skipped} failed={failed} ---\n")

print(f"\n  Результат: ✓ {migrated} мигрировано, → {skipped} пропущено, ✗ {failed} ошибок")
print(f"  Лог: {log_path}")
MIGRATE_PY
                        ok "Миграция завершена"
                    else
                        info "Нет клиентов для миграции"
                    fi
                fi
            fi
        fi
    fi

    save_state 7
fi

# ═══════════════════════════════════════════════════════════════
#  ШАГ 8: REALITY + WARP
# ═══════════════════════════════════════════════════════════════

if should_run 8; then
    step "Шаг 8/$TOTAL_STEPS: Настройка REALITY + WARP"

    cd "$PROJECT_DIR"

    if [[ "$DRY_RUN" == true ]]; then
        info "[dry-run] Пропуск настройки REALITY/WARP"
    else
        # REALITY
        substep "Настройка REALITY..."
        if [[ -f "scripts/05-setup-reality.sh" ]]; then
            if bash scripts/05-setup-reality.sh; then
                ok "REALITY настроен"
            else
                warn "Ошибка настройки REALITY — можно запустить позже: bash scripts/05-setup-reality.sh"
            fi
        else
            warn "Скрипт 05-setup-reality.sh не найден"
        fi

        # WARP (опционально)
        echo ""
        if confirm "Настроить WARP (для OpenAI/Netflix через Cloudflare)?"; then
            if [[ -f "scripts/06-setup-warp.sh" ]]; then
                if bash scripts/06-setup-warp.sh; then
                    ok "WARP настроен"
                else
                    warn "Ошибка настройки WARP — можно запустить позже: bash scripts/06-setup-warp.sh"
                fi
            else
                warn "Скрипт 06-setup-warp.sh не найден"
            fi
        else
            info "WARP пропущен"
        fi
    fi

    save_state 8
fi

# ═══════════════════════════════════════════════════════════════
#  ФИНАЛ
# ═══════════════════════════════════════════════════════════════

# Загружаем финальные значения
cd "$PROJECT_DIR"
set -a; source .env 2>/dev/null; set +a

REALITY_PUB=$(grep '^REALITY_PUBLIC_KEY=' .env 2>/dev/null | cut -d= -f2)
SERVER_IP="${SERVER_IP:-$(curl -sf --max-time 5 https://api.ipify.org 2>/dev/null || echo 'N/A')}"

rm -f "$STATE_FILE"

echo ""
echo -e "${BOLD}${GREEN}"
cat << 'DONE'
  ╔══════════════════════════════════════════════════════╗
  ║         🛸 VPNBZK — Установка завершена!            ║
  ╚══════════════════════════════════════════════════════╝
DONE
echo -e "${NC}"

echo -e "  ${BOLD}Адреса:${NC}"
echo -e "    Сайт:          https://${DOMAIN:-N/A}"
echo -e "    Админка:       https://${DOMAIN:-N/A}/admin"
echo -e "    Marzban:       https://${DOMAIN:-N/A}/marzban/"
echo -e "    IP сервера:    ${SERVER_IP}"
echo ""
echo -e "  ${BOLD}Учётные данные:${NC}"
echo -e "    Marzban:       admin / ${MARZBAN_ADMIN_PASS:-N/A}"
echo -e "    Суперадмин:    Telegram ID ${SUPERADMIN_TELEGRAM_ID:-N/A}"
echo -e "    Бот:           @${TELEGRAM_BOT_USERNAME:-N/A}"
echo ""
echo -e "  ${BOLD}VPN-протоколы:${NC}"
echo -e "    REALITY:       ${SERVER_IP}:${REALITY_PORT:-2053}"
if [[ -n "$REALITY_PUB" ]]; then
echo -e "    Public Key:    ${REALITY_PUB}"
fi
echo -e "    VLESS-WS:      https://${DOMAIN:-N/A}/vless-ws"
echo -e "    VLESS-gRPC:    https://${DOMAIN:-N/A}/vless-grpc"
echo -e "    Trojan-WS:     https://${DOMAIN:-N/A}/trojan-ws"
echo ""
echo -e "  ${BOLD}Управление:${NC}"
echo -e "    Логи:          docker compose logs -f"
echo -e "    Статус:        docker compose ps"
echo -e "    Перезапуск:    docker compose restart"
echo -e "    Обновление:    bash scripts/03-deploy.sh"
echo ""
echo -e "  ${BOLD}Файлы:${NC}"
echo -e "    Проект:        ${PROJECT_DIR}"
echo -e "    Лог установки: ${LOG_FILE}"
echo ""

if [[ "${HAS_3XUI:-false}" == true ]]; then
    echo -e "  ${YELLOW}${BOLD}3X-UI:${NC}"
    echo -e "    Статус: остановлен и отключён из автозапуска"
    echo -e "    Бэкап:  ${BACKUP_DIR:-$PROJECT_DIR/backups/}"
    echo -e "    Удалить полностью: systemctl stop x-ui; bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh) uninstall"
    echo ""
fi

echo -e "  ${GREEN}Всё готово! Настройте клиентов через Marzban-панель.${NC}"
echo ""
