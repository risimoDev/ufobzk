#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
#  update.sh — Быстрый деплой с текущего хоста
#  Запуск: sudo bash update.sh [--force]
# ─────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PROJECT_DIR="$SCRIPT_DIR"

exec bash "${SCRIPT_DIR}/scripts/08-deploy-main-server.sh" "$@"
