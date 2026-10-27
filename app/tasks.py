"""Фоновые задачи: сбор трафика из Xray API, проверка истечения ключей, автоотключение."""

import asyncio
import logging
from datetime import datetime


from app.models import SessionLocal, TrafficSnapshot, VPNKey
from app.remote_xray import sync_all_servers
from app.xray import (
    GEO_AUTO_BAD_COUNTRIES,
    GEO_EXIT_STATIC,
    GEO_IPV6_AUTO,
    GEO_NODE_NAME,
    GEO_TRANSIT_UUID,
    detect_google_country,
    get_all_xray_stats,
    get_geo_family,
    set_geo_family,
    sync_and_reload,
)

logger = logging.getLogger(__name__)

# Интервалы в секундах
TRAFFIC_COLLECT_INTERVAL = 300   # 5 минут
EXPIRE_CHECK_INTERVAL = 3600     # 1 час
REMOTE_SYNC_INTERVAL = 300       # 5 минут
BACKUP_INTERVAL = 86400          # 24 часа
SNAPSHOT_RETENTION_DAYS = 30     # хранить снимки трафика 30 дней (совпадает с макс. периодом аналитики)
SNAPSHOT_CLEANUP_INTERVAL = 86400  # чистка раз в сутки
GEO_AUTO_INTERVAL = 1800         # проверка страны Google — 30 минут
GEO_AUTO_RECHECK = 300           # повторная проверка перед переключением — 5 минут

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


def _resync_xray() -> None:
    db = SessionLocal()
    try:
        sync_and_reload(db)
    except Exception as e:
        logger.error("Ошибка синхронизации Xray после сбора трафика: %s", e)
    finally:
        db.close()


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

            # Ключ, перешедший лимит трафика, получает статус "limited" и
            # выпадает из конфига только при пересборке. Раньше её запускало лишь
            # истечение срока, и перелимитные ключи работали дальше. Если ни один
            # ключ лимит не перешёл, конфиг совпадёт с текущим и рестарта не будет.
            if updated:
                await asyncio.to_thread(_resync_xray)

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
                # В WAL-режиме удалённые страницы повторно используются для новых записей.
                # Вместо блокирующего монопольного VACUUM используем PRAGMA optimize и passive checkpoint.
                try:
                    from sqlalchemy import text as _sa_text
                    from app.models import engine as _engine
                    with _engine.connect().execution_options(isolation_level="AUTOCOMMIT") as _conn:
                        _conn.execute(_sa_text("PRAGMA optimize"))
                        _conn.execute(_sa_text("PRAGMA wal_checkpoint(PASSIVE)"))
                    logger.debug("Оптимизация SQLite и checkpoint выполнены успешно")
                except Exception as _ve:
                    logger.warning("Оптимизация SQLite пропущена: %s", _ve)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Ошибка очистки снимков трафика: %s", e)
        await asyncio.sleep(SNAPSHOT_CLEANUP_INTERVAL)


async def _notify_admin(text: str) -> None:
    """Короткое сообщение суперадмину в Telegram (без ошибок наружу)."""
    import httpx
    from app.backup import SUPERADMIN_CHAT_ID, TELEGRAM_TOKEN

    if not TELEGRAM_TOKEN or not SUPERADMIN_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": SUPERADMIN_CHAT_ID, "text": text},
            )
    except Exception as e:
        logger.warning("Не удалось отправить уведомление в Telegram: %s", e)


async def periodic_geo_auto() -> None:
    """GEO_IPV6=auto: держать Google на той семье адресов, которую он не видит как RU.

    Страна Google у главного сервера плавает (IPv4 и IPv6 уже менялись местами).
    Текущий выход остаётся, пока он в порядке — лишние переключения меняют
    регион в cookie пользователей. Переключение — только если текущий адрес
    плохой, другой хороший, и это подтвердилось двумя проверками подряд.
    """
    if not GEO_IPV6_AUTO:
        return
    if GEO_EXIT_STATIC or (GEO_NODE_NAME and GEO_TRANSIT_UUID):
        logger.info("GEO-AUTO выключен: задан GEO-EXIT, Google идёт через отдельный выход")
        return

    await asyncio.sleep(60)
    confirmed_bad = 0
    alerted = False
    while True:
        delay = GEO_AUTO_INTERVAL
        try:
            current = get_geo_family()
            other = "v6" if current == "v4" else "v4"
            cur_country, other_country = await asyncio.gather(
                asyncio.to_thread(detect_google_country, current),
                asyncio.to_thread(detect_google_country, other),
            )
            logger.info("GEO-AUTO: выход %s, Google видит %s=%s, %s=%s",
                        current, current, cur_country, other, other_country)

            current_ok = cur_country is not None and cur_country not in GEO_AUTO_BAD_COUNTRIES
            other_ok = other_country is not None and other_country not in GEO_AUTO_BAD_COUNTRIES
            if current_ok:
                confirmed_bad = 0
                alerted = False
            elif not other_ok:
                confirmed_bad = 0
                if not alerted and cur_country and other_country:
                    alerted = True
                    logger.error("GEO-AUTO: Google видит оба адреса как плохие (%s / %s)",
                                 cur_country, other_country)
                    await _notify_admin(
                        f"⚠️ Google видит главный сервер как {cur_country} по {current} "
                        f"и {other_country} по {other}. Gemini может не работать — "
                        "нужен выход через ноду (GEO_NODE_NAME)."
                    )
            else:
                confirmed_bad += 1
                if confirmed_bad < 2:
                    delay = GEO_AUTO_RECHECK
                else:
                    confirmed_bad = 0
                    set_geo_family(other)
                    await asyncio.to_thread(_resync_xray)
                    logger.warning("GEO-AUTO: Google переключён %s → %s (%s → %s)",
                                   current, other, cur_country, other_country)
                    await _notify_admin(
                        f"🔀 Google/Gemini переключён с {current} ({cur_country}) "
                        f"на {other} ({other_country})."
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Ошибка GEO-AUTO: %s", e)
        await asyncio.sleep(delay)


async def run_background_tasks() -> None:
    """Запускает все фоновые задачи параллельно."""
    tasks = [
        asyncio.create_task(periodic_traffic_collector()),
        asyncio.create_task(periodic_expire_checker()),
        asyncio.create_task(periodic_remote_sync()),
        asyncio.create_task(periodic_backup()),
        asyncio.create_task(periodic_snapshot_cleanup()),
        asyncio.create_task(periodic_geo_auto()),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            if not t.done():
                t.cancel()
