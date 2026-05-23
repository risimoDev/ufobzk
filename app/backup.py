"""Автоматический бэкап SQLite БД — локально + отправка в Telegram."""

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
    """Копирует vpnbzk.db → data/backups/vpnbzk_YYYYMMDD_HHMMSS.db.

    Возвращает путь к созданному файлу. Хранит последние KEEP_BACKUPS копий.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"vpnbzk_{ts}.db"
    shutil.copy2(DB_PATH, dst)
    _cleanup_old_backups()
    size_mb = dst.stat().st_size / 1_048_576
    logger.info("Backup created: %s (%.2f MB)", dst.name, size_mb)
    return dst


def _cleanup_old_backups() -> None:
    backups = sorted(
        BACKUP_DIR.glob("vpnbzk_*.db"),
        key=lambda p: p.stat().st_mtime,
    )
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
        logger.info("Deleted old backup: %s", old.name)


async def send_backup_to_telegram(filepath: Path) -> bool:
    """Отправляет файл бэкапа документом в Telegram суперадмину."""
    if not TELEGRAM_TOKEN or not SUPERADMIN_CHAT_ID:
        logger.warning("Telegram backup skipped: BOT_TOKEN или CHAT_ID не заданы")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    caption = (
        f"🗄 Бэкап БД\n"
        f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"📦 {filepath.stat().st_size / 1_048_576:.2f} MB"
    )
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            with open(filepath, "rb") as f:
                resp = await client.post(
                    url,
                    data={"chat_id": SUPERADMIN_CHAT_ID, "caption": caption},
                    files={"document": (filepath.name, f, "application/octet-stream")},
                )
        if resp.status_code == 200:
            logger.info("Backup sent to Telegram (%s)", filepath.name)
            return True
        logger.error("Telegram backup failed [%d]: %s", resp.status_code, resp.text[:300])
        return False
    except Exception as e:
        logger.error("Telegram backup error: %s", e)
        return False
