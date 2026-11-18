"""Автоматическая проверка доступности серверов и доменов в РФ через сервис Cheburcheck (cheburcheck.ru)."""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import Server, get_setting, set_setting

logger = logging.getLogger(__name__)

CHEBURCHECK_API_URL = "https://cheburcheck.ru/api/v1/check"
# Проверка раз в час (3600 сек). Раньше была 21600 (4 раза в сутки) — при
# активных блокировках ТСПУ узел оставался «мёртвым» в подписках до 6 часов.
try:
    CHEBURCHECK_INTERVAL = int(os.getenv("CHEBURCHECK_INTERVAL", "3600"))
except Exception:
    CHEBURCHECK_INTERVAL = 3600
MONITOR_EXTERNAL_TARGETS = os.getenv("MONITOR_EXTERNAL_TARGETS", "")

# TCP-проба портов ноды: кроме списка блокировок РКН делаем живой коннект
# к REALITY-порту ноды. Таймаут подобран так, чтобы весь параллельный аудит
# укладывался в ~15-20 секунд (как требует операционная процедура).
NODE_PROBE_TIMEOUT = float(os.getenv("CHEBURCHECK_PROBE_TIMEOUT", "12.0"))


async def probe_tcp(host: str, port: int, timeout: float = NODE_PROBE_TIMEOUT) -> tuple[bool, str]:
    """Живая TCP-проверка порта ноды (независимо от списков РКН).

    Ловит случаи, когда IP ещё не в списках, но порт уже дропается ТСПУ/DPI
    (RST или тишина на SYN): список чистый, а клиенты подключиться не могут.
    """
    import socket

    def _connect() -> tuple[bool, str]:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True, ""
        except OSError as e:
            return False, str(e) or type(e).__name__

    try:
        return await asyncio.wait_for(asyncio.to_thread(_connect), timeout=timeout + 2.0)
    except asyncio.TimeoutError:
        return False, f"probe timeout >{timeout:.0f}s"


async def probe_server_ports(db: Session) -> dict[str, dict[int, dict[str, Any]]]:
    """TCP-проба портов нод из БД. Возвращает {host: {port: {ok, error}}}.

    ВНИМАНИЕ: Проверяются ТОЛЬКО TCP-порты (REALITY). Hysteria2 и TUIC работают
    исключительно по UDP/QUIC — проверка их через TCP-хэндшейк гарантированно
    завершается сбоем и ошибочно метит живые ноды как заблокированные.
    """
    servers = db.query(Server).filter(Server.is_active == True).all()  # noqa: E712
    jobs: set[tuple[str, int]] = set()
    for s in servers:
        if s.host and s.host != "pending":
            port = s.reality_port or 443
            try:
                jobs.add((s.host, int(port)))
            except (ValueError, TypeError):
                continue
    if not jobs:
        return {}

    async def _run(host: str, port: int) -> tuple[str, int, bool, str]:
        ok, err = await probe_tcp(host, port)
        return host, port, ok, err

    probe_tasks = [_run(h, p) for h, p in jobs]
    probe_results = await asyncio.gather(*probe_tasks, return_exceptions=True)

    result: dict[str, dict[int, dict[str, Any]]] = {}
    for item in probe_results:
        if isinstance(item, tuple) and len(item) == 4:
            host, port, ok, err = item
            result.setdefault(host, {})[port] = {"ok": ok, "error": err}
    return result


async def check_target(target_or_client: Any, target: str | None = None) -> dict[str, Any]:
    """Проверить один IP или домен через Cheburcheck API."""
    if isinstance(target_or_client, str):
        target_str = target_or_client
        client = None
    else:
        client = target_or_client
        target_str = target or ""

    target_str = target_str.strip()
    if not target_str:
        return {"target": target_str, "status": "error", "error": "Пустой адрес"}

    if client is None:
        try:
            async with httpx.AsyncClient(timeout=15.0, trust_env=False, headers={"User-Agent": "VPNBZK-Monitor/1.0"}) as cl:
                return await check_target(cl, target_str)
        except Exception as e:
            return {"target": target_str, "blocked": False, "status": "error", "error": str(e), "checked_at": datetime.now(timezone.utc).isoformat()}

    try:
        resp = await client.get(
            CHEBURCHECK_API_URL,
            params={"target": target_str},
            timeout=12.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            is_blocked = bool(data.get("blocked", False))
            blocked_subnets = data.get("blocked_subnets", [])
            geo = data.get("geo", {}) or {}
            return {
                "target": target_str,
                "blocked": is_blocked,
                "status": "blocked" if is_blocked else "clean",
                "blocked_subnets": blocked_subnets,
                "country": geo.get("country_code", ""),
                "asn": geo.get("asn", ""),
                "org": geo.get("organisation", ""),
                "error": None,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            return {
                "target": target_str,
                "blocked": False,
                "status": "error",
                "error": f"HTTP {resp.status_code}",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
    except Exception as e:
        logger.warning("Ошибка проверки Cheburcheck для %s: %s", target_str, e)
        return {
            "target": target_str,
            "blocked": False,
            "status": "error",
            "error": str(e),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


def get_monitored_targets(db: Session) -> list[dict[str, str]]:
    """Сформировать список всех целей инфраструктуры для проверки."""
    targets = []
    seen = set()

    def _add(t: str, name: str, role: str) -> None:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            targets.append({"target": t, "name": name, "role": role})

    # 1. Основной сервер (Главный IP)
    main_ip = os.getenv("MAIN_WEB_IP") or os.getenv("NL_SERVER_IP") or "193.47.60.116"
    _add(main_ip, "Master Server (Main IP)", "master_main")

    # 2. Второй IP основного сервера (VLESS-REALITY)
    reality_ip = os.getenv("REALITY_BIND_IP") or "193.47.60.232"
    _add(reality_ip, "Master Server (REALITY IP)", "master_reality")

    # 3. Домен
    domain = os.getenv("DOMAIN") or "ufolabs.online"
    _add(domain, f"Домен сервиса ({domain})", "domain")

    # 4. RU-сервер каскада
    ru_ip = os.getenv("RU_SERVER_IP") or "194.226.54.42"
    _add(ru_ip, "RU Cascade Server", "ru_cascade")

    # 5. Все подключенные активные ноды из базы
    servers = db.query(Server).filter(Server.is_active == True).all()  # noqa: E712
    for s in servers:
        _add(s.host, f"Нода {s.name} ({s.region.upper()})", f"node_{s.role}")

    # 6. Внешние дополнительные проекты/сервера из .env
    external = MONITOR_EXTERNAL_TARGETS or os.getenv("MONITOR_TARGETS", "")
    for ext in external.split(","):
        ext = ext.strip()
        if ext:
            _add(ext, f"Внешний сервер/проект ({ext})", "external")

    return targets


async def run_cheburcheck_audit(db: Session, notify: bool = True) -> dict[str, Any]:
    """Выполнить полный аудит всех целей инфраструктуры и сохранить результат.

    Состав проверки (весь аудит идёт параллельно и укладывается в ~15-20 сек):
    1. Cheburcheck API — есть ли IP/домен в списках блокировок РКН.
    2. Живая TCP-проба портов нод из БД — ловит DPI-дропы, которых ещё
       нет в списках.
    Результаты со статусом error (API недоступен) НЕ считаются «чистыми» —
    они попадают в алерт, иначе отказ cheburcheck.ru выглядит как «всё чисто».
    """
    targets = get_monitored_targets(db)
    started = datetime.now(timezone.utc)

    async with httpx.AsyncClient(timeout=15.0, trust_env=False, headers={"User-Agent": "VPNBZK-Monitor/1.0"}) as client:
        tasks = [check_target(client, item["target"]) for item in targets]
        checked = await asyncio.gather(*tasks, return_exceptions=True)
        port_probes = await probe_server_ports(db)

    results = []
    for item, res in zip(targets, checked):
        if isinstance(res, Exception):
            entry = {
                "target": item["target"],
                "name": item["name"],
                "role": item["role"],
                "blocked": False,
                "status": "error",
                "error": str(res),
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            assert isinstance(res, dict)
            entry = {**item, **res}

        # Прицепляем живую пробу портов для нод из БД
        if item["role"].startswith("node_"):
            probes = port_probes.get(item["target"], {})
            dead_ports = [p for p, r in probes.items() if not r.get("ok")]
            entry["dead_ports"] = dead_ports
            if dead_ports:
                entry["port_unreachable"] = True
                if entry.get("status") == "clean":
                    entry["status"] = "port_down"
                    # IP не в списках, но порты молчат — фактически нода недоступна
                    entry["blocked"] = True
                    entry["blocked_reason"] = f"TCP-порты недоступны: {dead_ports}"
        results.append(entry)

    blocked_count = sum(1 for r in results if r.get("blocked"))
    error_count = sum(1 for r in results if r.get("status") == "error")
    now_iso = datetime.now(timezone.utc).isoformat()

    summary = {
        "checked_at": now_iso,
        "duration_sec": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "total": len(results),
        "blocked_count": blocked_count,
        "error_count": error_count,
        "has_blocked": blocked_count > 0,
        "results": results,
    }

    # Получаем предыдущий статус ДО сохранения нового (для выявления изменений)
    prev_raw = None
    try:
        prev_raw = get_setting(db, "cheburcheck_last_audit")
    except Exception:
        pass
    prev_blocked: set[str] = set()
    if prev_raw:
        try:
            prev = json.loads(prev_raw) if isinstance(prev_raw, str) else prev_raw
            prev_blocked = {r.get("target") for r in prev.get("results", []) if r.get("blocked")}
        except Exception:
            prev_blocked = set()

    # Сохраняем в базу данных в AppSetting
    try:
        set_setting(db, "cheburcheck_last_audit", json.dumps(summary, ensure_ascii=False))
        db.commit()
    except Exception as e:
        logger.error("Не удалось сохранить результат Cheburcheck в AppSetting: %s", e)

    # Автопометка нод: is_blocked_ru ставится/снимается по результату аудита,
    # чтобы подписки клиентов перестали выдавать заблокированную ноду.
    try:
        _update_blocked_flags(db, summary)
    except Exception as e:
        logger.error("Не удалось обновить is_blocked_ru по аудиту: %s", e)

    # Оповещение: блокировки, ошибки API и смена состояния (без ежедневного
    # повторения одного и того же алерта).
    if notify:
        cur_blocked = {r["target"] for r in results if r.get("blocked")}
        if blocked_count > 0 and (cur_blocked != prev_blocked or error_count > 0):
            await notify_telegram_alert(summary)
        elif error_count > 0:
            logger.warning("Cheburcheck: %d целей не удалось проверить (API недоступен?)", error_count)
        elif cur_blocked != prev_blocked:
            await notify_telegram_alert(summary, always=True)

    return summary


def _update_blocked_flags(db: Session, summary: dict[str, Any]) -> None:
    """Синхронизировать Server.is_blocked_ru с результатом аудита."""
    blocked_hosts = {r["target"] for r in summary.get("results", []) if r.get("blocked")}
    servers = db.query(Server).all()
    changed = False
    for s in servers:
        should = s.host in blocked_hosts
        if bool(s.is_blocked_ru) != should:
            s.is_blocked_ru = should
            changed = True
            logger.warning("Нода %s (%s): is_blocked_ru → %s", s.name, s.host, should)
    if changed:
        db.commit()


async def notify_telegram_alert(summary: dict[str, Any], always: bool = False) -> None:
    """Отправить уведомление суперадмину о заблокированных узлах."""
    from app.backup import SUPERADMIN_CHAT_ID, TELEGRAM_TOKEN

    if not TELEGRAM_TOKEN or not SUPERADMIN_CHAT_ID:
        return

    blocked_items = [r for r in summary.get("results", []) if r.get("blocked")]
    if not blocked_items and not always:
        return

    if blocked_items:
        lines = [
            "🚨 <b>ВНИМАНИЕ! Обнаружена блокировка узлов в РФ (Cheburcheck)</b>",
            f"📅 <i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</i>",
            "",
        ]
        for b in blocked_items:
            lines.append(f"❌ <b>{b.get('name')}</b> (<code>{b.get('target')}</code>)")
            if b.get("blocked_subnets"):
                subnets = ", ".join(b.get("blocked_subnets", []))
                lines.append(f"   ↳ Блокировка подсети: <code>{subnets}</code>")
            if b.get("country"):
                lines.append(f"   ↳ Гео: {b.get('country')} | {b.get('org', '')}")
            lines.append("")
        lines.append("⚠️ <i>Рекомендуется заменить заблокированные ноды или скрыть их в админ-панели.</i>")
    else:
        lines = [
            "✅ <b>Cheburcheck: все узлы инфраструктуры чисты</b>",
            f"📅 <i>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC</i>",
            f"Проверено узлов: {summary.get('total', 0)}",
            "Блокировок со стороны РКН / ТСПУ не обнаружено.",
        ]

    text = "\n".join(lines)

    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": SUPERADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"},
            )
    except Exception as e:
        logger.warning("Не удалось отправить алерт Cheburcheck в Telegram: %s", e)
