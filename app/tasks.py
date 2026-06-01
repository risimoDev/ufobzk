"""Фоновые задачи: сбор трафика из Xray API, проверка истечения ключей, автоотключение."""

import asyncio
import logging
from datetime import datetime


from app.models import SessionLocal, TrafficSnapshot, VPNKey
from app.remote_xray import sync_all_servers
from app.xray import get_all_xray_stats, sync_and_reload

logger = logging.getLogger(__name__)

# Интервалы в секундах
TRAFFIC_COLLECT_INTERVAL = 300   # 5 минут
EXPIRE_CHECK_INTERVAL = 3600     # 1 час
REMOTE_SYNC_INTERVAL = 300       # 5 минут
BACKUP_INTERVAL = 86400          # 24 часа
SNAPSHOT_RETENTION_DAYS = 90     # хранить снимки трафика 90 дней
SNAPSHOT_CLEANUP_INTERVAL = 86400  # чистка раз в сутки


async def periodic_traffic_collector() -> None:
    """Периодически опрашивает Xray API и обновляет data_used в БД.

    Один batch-вызов на все ключи вместо N отдельных subprocess.
    """
    consecutive_empty = 0
    while True:
        try:
            await asyncio.sleep(TRAFFIC_COLLECT_INTERVAL)

            # Один вызов — все UUID сразу
            all_stats = await asyncio.to_thread(get_all_xray_stats)
            if not all_stats:
                consecutive_empty += 1
                # Первые 2 пустых результата — debug (нормально при старте),
                # далее — warning каждые 30 минут
                if consecutive_empty == 1:
                    logger.debug("Xray stats пусты — ожидание трафика или Xray ещё стартует")
                elif consecutive_empty % 6 == 0:
                    logger.warning(
                        "Xray stats недоступны %d циклов подряд (%d мин) — "
                        "проверьте XRAY_API_ADDR=%s и состояние контейнера xray",
                        consecutive_empty,
                        consecutive_empty * TRAFFIC_COLLECT_INTERVAL // 60,
                        __import__("os").getenv("XRAY_API_ADDR", "xray:10085"),
                    )
                continue
            consecutive_empty = 0

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
                # naive UTC для корректного строкового сравнения в SQLite —
                # timezone-aware datetime добавляет "+00:00" суффикс, что ломает
                # лексикографическое сравнение с naive-значениями в БД.
                now_naive = datetime.utcnow()
                expired_keys = db.query(VPNKey).filter(
                    VPNKey.is_active == True,  # noqa: E712
                    VPNKey.expire_at != None,  # noqa: E711
                    VPNKey.expire_at < now_naive,
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


async def periodic_snapshot_cleanup() -> None:
    """Удаляет снимки трафика старше SNAPSHOT_RETENTION_DAYS дней."""
    await asyncio.sleep(3600 * 2)  # первый прогон через 2 часа после старта
    while True:
        try:
            from datetime import timedelta
            cutoff = (datetime.utcnow() - timedelta(days=SNAPSHOT_RETENTION_DAYS))
            db = SessionLocal()
            try:
                deleted = db.query(TrafficSnapshot).filter(
                    TrafficSnapshot.recorded_at < cutoff
                ).delete(synchronize_session=False)
                if deleted:
                    db.commit()
                    logger.info("Удалено %d устаревших снимков трафика (старше %d дней)", deleted, SNAPSHOT_RETENTION_DAYS)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Ошибка очистки снимков трафика: %s", e)
        await asyncio.sleep(SNAPSHOT_CLEANUP_INTERVAL)


async def run_background_tasks() -> None:
    """Запускает все фоновые задачи параллельно."""
    tasks = [
        asyncio.create_task(periodic_traffic_collector()),
        asyncio.create_task(periodic_expire_checker()),
        asyncio.create_task(periodic_remote_sync()),
        asyncio.create_task(periodic_backup()),
        asyncio.create_task(periodic_snapshot_cleanup()),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
