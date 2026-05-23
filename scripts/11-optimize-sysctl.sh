#!/bin/bash
# =============================================================
#  VPNBZK — Оптимизация сетевого стека хоста для VPN трафика
#  Запускать один раз на сервере от root.
#  Настройки сохраняются после перезагрузки.
#  Применяет BBR + расширенные TCP буферы + оптимизации.
# =============================================================

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Запустите от root: sudo bash scripts/11-optimize-sysctl.sh"
  exit 1
fi

echo "=== VPN сетевые оптимизации ==="

# Проверка поддержки BBR ядром
echo ""
echo "[1/4] Проверка BBR..."
if modprobe tcp_bbr 2>/dev/null; then
  echo "  ✓ BBR модуль загружен"
else
  echo "  ! BBR не поддерживается ядром (нужен Linux 4.9+). Пропускаем."
fi

# Проверка текущего алгоритма
CURRENT=$(cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null || echo "unknown")
echo "  Текущий алгоритм: $CURRENT"

echo ""
echo "[2/4] Запись конфигурации sysctl..."
SYSCTL_FILE="/etc/sysctl.d/99-vpn-optimizations.conf"

cat > "$SYSCTL_FILE" << 'EOF'
# =============================================
# VPNBZK — сетевые оптимизации для VPN хоста
# =============================================

# BBR congestion control (лучший для VPN трафика, +20-40% throughput)
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr

# Расширенные TCP буферы (для высокоскоростных соединений)
net.core.rmem_max=134217728
net.core.wmem_max=134217728
net.ipv4.tcp_rmem=4096 87380 67108864
net.ipv4.tcp_wmem=4096 65536 67108864

# UDP буферы (для QUIC и будущего Hysteria2)
net.core.rmem_default=26214400
net.core.wmem_default=26214400

# Очередь соединений
net.core.somaxconn=65535
net.ipv4.tcp_max_syn_backlog=65535
net.core.netdev_max_backlog=65535

# TIME_WAIT оптимизация (переиспользование сокетов)
net.ipv4.tcp_tw_reuse=1
net.ipv4.tcp_fin_timeout=15

# IP-форвардинг для VPN роутинга
net.ipv4.ip_forward=1

# Защита от SYN-flood
net.ipv4.tcp_syncookies=1
EOF

echo "  ✓ Записано в $SYSCTL_FILE"

echo ""
echo "[3/4] Применение настроек..."
sysctl -p "$SYSCTL_FILE" 2>&1 | grep -v "^#" | head -20

echo ""
echo "[4/4] Проверка результата:"
ALGO=$(cat /proc/sys/net/ipv4/tcp_congestion_control 2>/dev/null || echo "?")
QDISC=$(cat /proc/sys/net/core/default_qdisc 2>/dev/null || echo "?")
RMEM=$(cat /proc/sys/net/core/rmem_max 2>/dev/null || echo "?")
FWD=$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || echo "?")

echo "  tcp_congestion_control : $ALGO $([ "$ALGO" = "bbr" ] && echo "✓" || echo "✗ BBR недоступен")"
echo "  default_qdisc          : $QDISC $([ "$QDISC" = "fq" ] && echo "✓" || echo "✗")"
echo "  rmem_max               : $RMEM $([ "$RMEM" = "134217728" ] && echo "✓" || echo "✗")"
echo "  ip_forward             : $FWD $([ "$FWD" = "1" ] && echo "✓" || echo "✗")"

echo ""
echo "=== Готово. Настройки применены и сохранены. ==="
echo "Перезагрузка не требуется."
