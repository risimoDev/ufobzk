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
SNAPSHOT_RETENTION_DAYS = 30     # хранить снимки трафика 30 дней (совпадает с макс. периодом аналитики)
SNAPSHOT_CLEANUP_INTERVAL = 86400  # чистка раз в сутки

# Последнее «сырое» значение счётчика Xray по UUID (в памяти процесса).
# Нужно для корректного подсчёта дельт: счётчики Xray обнуляются при рестарте
# контейнера, поэтому нельзя считать их вечно растущими.
_last_raw: dict[str, int] = {}


def collect_and_store_traffic(db, all_stats: dict[str, int]) -> int:
    """Применить статистику Xray к data_used и записать почасовой снимок.

    - Накопительный учёт: data_used += дельта (а не = текущему счётчику Xray),
      что переживает рестарты Xray (счётчики в памяти обнуляются).
    - Детект сброса: если raw < предыдущего — счётчик обнулён, дельта = raw.
    - Снимок один на ключ в час (upsert в строку с recorded_at = начало часа),
      чтобы не раздувать БД (раньше писалось каждые 5 минут).

    Возвращает количество ключей, по которым был прирост.
    """
    keys = db.query(VPNKey).filter(VPNKey.is_active == True).all()  # noqa: E712
    now = datetime.utcnow()  # naive UTC — совместимо с SQLite DateTime
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    updated = 0
    for key in keys:
        if key.protocol != "vless":
            continue
        new_raw = all_stats.get(key.uuid, 0)
        prev = _last_raw.get(key.uuid)
        _last_raw[key.uuid] = new_raw
        if prev is None:
            # Первая засечка после старта процесса — задаём базовую линию,
            # дельту не считаем (иначе можно задвоить накопленное в Xray).
            continue
        delta = new_raw - prev if new_raw >= prev else new_raw  # reset → дельта = raw
        if delta <= 0:
            continue
        key.data_used = (key.data_used or 0) + delta
        snap = db.query(TrafficSnapshot).filter(
            TrafficSnapshot.vpn_key_id == key.id,
            TrafficSnapshot.recorded_at == hour_start,
        ).first()
        if snap:
            snap.bytes_delta = (snap.bytes_delta or 0) + delta
            snap.total_bytes = key.data_used
        else:
            db.add(TrafficSnapshot(
                vpn_key_id=key.id,
                bytes_delta=delta,
                total_bytes=key.data_used,
                recorded_at=hour_start,
            ))
        updated += 1
    if updated:
        db.commit()
    return updated


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
                updated = collect_and_store_traffic(db, all_stats)
                if updated:
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
    """Удаляет снимки трафика старше SNAPSHOT_RETENTION_DAYS дней и сжимает БД.

    Первый прогон — через 5 минут после старта (а не 2 часа): при частых
    рестартах долгая задержка означала, что чистка фактически не запускалась.
    После удаления выполняется VACUUM, иначе файл SQLite не уменьшается.
    """
    await asyncio.sleep(300)
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
            if deleted:
                # VACUUM нельзя выполнять внутри транзакции — отдельное соединение
                # в режиме AUTOCOMMIT. Освобождает место на диске после удаления.
                try:
                    from sqlalchemy import text as _sa_text
                    from app.models import engine as _engine
                    with _engine.connect().execution_options(isolation_level="AUTOCOMMIT") as _conn:
                        _conn.execute(_sa_text("VACUUM"))
                    logger.info("VACUUM выполнен — файл БД сжат")
                except Exception as _ve:
                    logger.warning("VACUUM пропущен: %s", _ve)
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
