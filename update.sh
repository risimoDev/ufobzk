#!/usr/bin/env bash
# ─────────────────────────────────────────────────
#  update.sh — Обновление проекта из git-репо
#  Запуск: cd /root/temp/ufobzk && bash update.sh
# ─────────────────────────────────────────────────
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
step()  { echo -e "${CYAN}→${NC} $*"; }
die()   { echo -e "${RED}[✗]${NC} $*"; exit 1; }

PROJECT_DIR="/opt/vpnbzk"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ -f "$REPO_DIR/docker-compose.yml" ]] || die "Запускайте из корня репозитория"
[[ -d "$PROJECT_DIR" ]] || die "$PROJECT_DIR не существует. Сначала запустите setup.sh"

# ── Git pull ──
step "git pull..."
cd "$REPO_DIR"
git pull 2>&1 | head -20
info "Репозиторий обновлён"

# ── Синхронизация файлов ──
step "Синхронизация → $PROJECT_DIR"
tar -C "$REPO_DIR" \
    --exclude='.git' --exclude='data' --exclude='.env' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.warp' \
    -cf - . | tar -C "$PROJECT_DIR" -xf -
info "Файлы обновлены"

# ── Что перезапускать? ──
cd "$PROJECT_DIR"

NEED_BUILD=false
NEED_RESTART=false

# Проверяем изменились ли файлы приложения или Docker
CHANGED=$(cd "$REPO_DIR" && git diff --name-only HEAD~1 HEAD 2>/dev/null || echo "")

if echo "$CHANGED" | grep -qE '^(Dockerfile|docker/|requirements\.txt|app/)'; then
    NEED_BUILD=true
fi

if echo "$CHANGED" | grep -qE '^(docker-compose\.yml|nginx/|xray_config\.json)'; then
    NEED_RESTART=true
fi

if echo "$CHANGED" | grep -qE '^app/'; then
    NEED_RESTART=true
fi

# ── Применяем ──
if [[ "$NEED_BUILD" == true ]]; then
    step "Пересборка Docker-образов..."
    docker compose build --quiet
    info "Образы пересобраны"
    NEED_RESTART=true
fi

if [[ "$NEED_RESTART" == true ]]; then
    step "Перезапуск стека..."
    docker compose up -d --force-recreate
    info "Стек перезапущен"
else
    # Даже если структурных изменений нет — перезапустим app на всякий
    if docker ps --format '{{.Names}}' | grep -q 'vpnbzk-app'; then
        step "Перезапуск приложения..."
        docker compose restart ufo-app
        info "Приложение перезапущено"
    fi
fi

echo ""
info "Обновление завершено!"
docker compose ps --format "table {{.Name}}\t{{.Status}}"
