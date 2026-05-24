"""Фоновые задачи: сбор трафика из Xray API, проверка истечения ключей, автоотключение."""

import asyncio
import logging
from datetime import datetime, timezone


from app.models import SessionLocal, TrafficSnapshot, VPNKey
from app.remote_xray import sync_all_servers
from app.xray import get_all_xray_stats, sync_and_reload

logger = logging.getLogger(__name__)

# Интервалы в секундах
TRAFFIC_COLLECT_INTERVAL = 300   # 5 минут
EXPIRE_CHECK_INTERVAL = 3600     # 1 час
REMOTE_SYNC_INTERVAL = 300       # 5 минут
BACKUP_INTERVAL = 86400          # 24 часа


async def periodic_traffic_collector() -> None:
    """Периодически опрашивает Xray API и обновляет data_used в БД.

    Один batch-вызов на все ключи вместо N отдельных subprocess.
    """
    while True:
        try:
            await asyncio.sleep(TRAFFIC_COLLECT_INTERVAL)

            # Один вызов — все UUID сразу
            all_stats = await asyncio.to_thread(get_all_xray_stats)
            if not all_stats:
                logger.debug("Xray stats пусты или недоступны")
                continue

            db = SessionLocal()
            try:
                keys = db.query(VPNKey).filter(VPNKey.is_active == True).all()  # noqa: E712
                updated = 0
                now = datetime.utcnow()  # naive UTC — совместимо с SQLite DateTime
                for key in keys:
                    if key.protocol != "vless":
                        continue
                    new_total = all_stats.get(key.uuid, 0)
                    if new_total > (key.data_used or 0):
                        delta = new_total - (key.data_used or 0)
                        key.data_used = new_total
                        db.add(TrafficSnapshot(
                            vpn_key_id=key.id,
                            bytes_delta=delta,
                            total_bytes=new_total,
                            recorded_at=now,
                        ))
                        updated += 1
                if updated:
                    db.commit()
                    logger.info("Трафик обновлён для %d ключей", updated)
            finally:
                db.close()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Ошибка сбора трафика: %s", e)


async def periodic_expire_checker() -> None:
    """Периодически проверяет истёкшие ключи и отключает их."""
    while True:
        try:
            await asyncio.sleep(EXPIRE_CHECK_INTERVAL)
            db = SessionLocal()
            try:
                now = datetime.now(timezone.utc)
                expired_keys = db.query(VPNKey).filter(
                    VPNKey.is_active == True,  # noqa: E712
                    VPNKey.expire_at != None,  # noqa: E711
                    VPNKey.expire_at < now,
                ).all()

                if not expired_keys:
                    continue

                for key in expired_keys:
                    key.is_active = False
                    logger.info("Ключ %s истёк (expire_at=%s), отключён", key.uuid, key.expire_at)

                db.commit()

                # Пересинхронизируем Xray конфиг
                try:
                    sync_and_reload(db)
                except Exception as e:
                    logger.error("Ошибка синхронизации Xray после отключения ключей: %s", e)

            finally:
                db.close()

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Ошибка проверки истечения ключей: %s", e)


async def periodic_remote_sync() -> None:
    """Периодически синхронизирует конфиг на все remote Xray серверы."""
    while True:
        try:
            await asyncio.sleep(REMOTE_SYNC_INTERVAL)
            db = SessionLocal()
            try:
                results = await sync_all_servers(db)
                ok_count = sum(1 for v in results.values() if v)
                if results:
                    logger.info("Синхронизация серверов: %d/%d OK", ok_count, len(results))
            finally:
                db.close()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Ошибка синхронизации remote серверов: %s", e)


async def periodic_backup() -> None:
    """Раз в 24 часа создаёт бэкап БД локально и отправляет в Telegram."""
    await asyncio.sleep(3600)  # первый бэкап через час после старта
    while True:
        try:
            from app.backup import create_local_backup, send_backup_to_telegram
            filepath = await asyncio.to_thread(create_local_backup)
            await send_backup_to_telegram(filepath)
        except Exception as e:
            logger.error("Backup task error: %s", e)
        await asyncio.sleep(BACKUP_INTERVAL)


async def run_background_tasks() -> None:
    """Запускает все фоновые задачи параллельно."""
    tasks = [
        asyncio.create_task(periodic_traffic_collector()),
        asyncio.create_task(periodic_expire_checker()),
        asyncio.create_task(periodic_remote_sync()),
        asyncio.create_task(periodic_backup()),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
