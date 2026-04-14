#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  01-prepare-server.sh — Первичная подготовка нового сервера
# ═══════════════════════════════════════════════════════════
#  Что делает:
#   • Обновляет систему
#   • Устанавливает Docker + Docker Compose
#   • Настраивает UFW (SSH + HTTP + HTTPS)
#   • Конфигурирует swap (если нет)
#   • Включает автообновление безопасности
#   • Создаёт пользователя deploy (опционально)
#
#  Запуск: curl -sL <url> | sudo bash
#     или: sudo bash scripts/01-prepare-server.sh
#
#  Поддержка: Ubuntu 22.04 / 24.04, Debian 11 / 12
# ═══════════════════════════════════════════════════════════

set -euo pipefail

# ── Цвета ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[•]${NC} $*"; }
ok()   { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[✗]${NC} $*" >&2; }

# ── Проверки ──

if [ "$(id -u)" -ne 0 ]; then
    err "Запустите от root: sudo bash $0"
    exit 1
fi

if ! grep -qiE 'ubuntu|debian' /etc/os-release 2>/dev/null; then
    err "Поддерживаются только Ubuntu / Debian"
    exit 1
fi

echo ""
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo -e "${CYAN}  Подготовка сервера для проекта НИИ АЯ    ${NC}"
echo -e "${CYAN}═══════════════════════════════════════════${NC}"
echo ""

# ══════════════════════════════════════════
# 1. Обновление системы
# ══════════════════════════════════════════

log "Обновление системы..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
ok "Система обновлена"

# ══════════════════════════════════════════
# 2. Базовые пакеты
# ══════════════════════════════════════════

log "Установка базовых пакетов..."
apt-get install -y -qq \
    ca-certificates curl gnupg lsb-release \
    git htop nano wget unzip \
    fail2ban ufw \
    unattended-upgrades apt-listchanges
ok "Базовые пакеты установлены"

# ══════════════════════════════════════════
# 3. Docker
# ══════════════════════════════════════════

if command -v docker &>/dev/null; then
    ok "Docker уже установлен: $(docker --version)"
else
    log "Установка Docker..."

    install -m 0755 -d /etc/apt/keyrings

    DISTRO=$(. /etc/os-release && echo "$ID")
    curl -fsSL "https://download.docker.com/linux/$DISTRO/gpg" \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
      https://download.docker.com/linux/$DISTRO \
      $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    ok "Docker установлен: $(docker --version)"
fi

# Docker Compose check
if docker compose version &>/dev/null; then
    ok "Docker Compose: $(docker compose version --short)"
else
    err "Docker Compose plugin не найден!"
    exit 1
fi

systemctl enable docker --now

# ══════════════════════════════════════════
# 4. Пользователь deploy (опционально)
# ══════════════════════════════════════════

DEPLOY_USER="deploy"
if id "$DEPLOY_USER" &>/dev/null; then
    ok "Пользователь '$DEPLOY_USER' уже существует"
else
    read -rp "$(echo -e "${YELLOW}Создать пользователя '$DEPLOY_USER'? [y/N]: ${NC}")" CREATE_USER
    if [[ "${CREATE_USER,,}" == "y" ]]; then
        useradd -m -s /bin/bash -G docker,sudo "$DEPLOY_USER"
        passwd "$DEPLOY_USER"
        ok "Пользователь '$DEPLOY_USER' создан и добавлен в группы docker, sudo"
    else
        warn "Пропущено. Добавьте текущего пользователя в группу docker вручную при необходимости."
    fi
fi

# ══════════════════════════════════════════
# 5. UFW Firewall
# ══════════════════════════════════════════

log "Настройка UFW..."

ufw --force reset >/dev/null 2>&1
ufw default deny incoming
ufw default allow outgoing

ufw allow ssh comment 'SSH'
ufw allow 80/tcp comment 'HTTP'
ufw allow 443/tcp comment 'HTTPS'

# Xray порты (распространённые для VLESS/VMess)
read -rp "$(echo -e "${YELLOW}Открыть Xray-порт 8443/tcp? [y/N]: ${NC}")" OPEN_XRAY
if [[ "${OPEN_XRAY,,}" == "y" ]]; then
    ufw allow 8443/tcp comment 'Xray'
    ok "Порт 8443 открыт"
fi

ufw --force enable
ok "Firewall включён"
ufw status numbered

# ══════════════════════════════════════════
# 6. Fail2ban
# ══════════════════════════════════════════

log "Настройка Fail2ban..."
systemctl enable fail2ban --now

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

# Копируем фильтр и jail для VPNBZK (если скрипт запущен из репо)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
if [ -d "$REPO_DIR/fail2ban" ]; then
    cp "$REPO_DIR/fail2ban/filter.d/vpnbzk.conf" /etc/fail2ban/filter.d/ 2>/dev/null || true
    cp "$REPO_DIR/fail2ban/jail.d/vpnbzk.conf" /etc/fail2ban/jail.d/ 2>/dev/null || true
    log "Установлен fail2ban-фильтр VPNBZK"
fi

systemctl restart fail2ban
ok "Fail2ban настроен"

# ══════════════════════════════════════════
# 7. Swap (если нет и RAM < 2GB)
# ══════════════════════════════════════════

TOTAL_RAM_MB=$(free -m | awk '/^Mem:/ {print $2}')
SWAP_MB=$(free -m | awk '/^Swap:/ {print $2}')

if [ "$SWAP_MB" -lt 100 ] && [ "$TOTAL_RAM_MB" -lt 2048 ]; then
    log "Создание swap (2GB)..."
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    # Оптимизация
    sysctl vm.swappiness=10
    echo 'vm.swappiness=10' >> /etc/sysctl.conf
    ok "Swap 2GB создан"
else
    ok "Swap уже есть (${SWAP_MB}MB) или RAM достаточно (${TOTAL_RAM_MB}MB)"
fi

# ══════════════════════════════════════════
# 8. Автообновление безопасности
# ══════════════════════════════════════════

log "Включение автообновлений безопасности..."
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
ok "Автообновления включены"

# ══════════════════════════════════════════
# 9. Системные лимиты для Docker/Xray
# ══════════════════════════════════════════

log "Оптимизация системных лимитов..."
cat > /etc/sysctl.d/99-vpnbzk.conf <<'EOF'
# Увеличение лимитов для TCP/Proxy
net.core.somaxconn = 4096
net.ipv4.tcp_max_syn_backlog = 4096
net.ipv4.ip_local_port_range = 1024 65535
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.core.netdev_max_backlog = 4096
# Файловые дескрипторы
fs.file-max = 1048576
EOF
sysctl --system -q
ok "Лимиты применены"

# ══════════════════════════════════════════
# 10. Итог
# ══════════════════════════════════════════

echo ""
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo -e "${GREEN}  ✅ Сервер подготовлен!                   ${NC}"
echo -e "${GREEN}═══════════════════════════════════════════${NC}"
echo ""
echo -e "  Docker:      $(docker --version | cut -d' ' -f3 | tr -d ',')"
echo -e "  Compose:     $(docker compose version --short)"
echo -e "  RAM:         ${TOTAL_RAM_MB} MB"
echo -e "  Swap:        $(free -m | awk '/^Swap:/ {print $2}') MB"
echo -e "  Firewall:    $(ufw status | head -1)"
echo ""
echo -e "  ${CYAN}Следующий шаг:${NC}"
echo -e "  bash scripts/02-install.sh"
echo ""
