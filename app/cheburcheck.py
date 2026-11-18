"""Автоматическая проверка доступности серверов и доменов в РФ через сервис Cheburcheck (cheburcheck.ru)."""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.models import AppSetting, Server, get_setting, set_setting

logger = logging.getLogger(__name__)

CHEBURCHECK_API_URL = "https://cheburcheck.ru/api/v1/check"
# По умолчанию проверка 4 раза в сутки (каждые 6 часов = 21600 сек)
try:
    CHEBURCHECK_INTERVAL = int(os.getenv("CHEBURCHECK_INTERVAL", "21600"))
except Exception:
    CHEBURCHECK_INTERVAL = 21600
MONITOR_EXTERNAL_TARGETS = os.getenv("MONITOR_EXTERNAL_TARGETS", "")


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
                "target": target,
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
                "target": target,
                "blocked": False,
                "status": "error",
                "error": f"HTTP {resp.status_code}",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
    except Exception as e:
        logger.warning("Ошибка проверки Cheburcheck для %s: %s", target, e)
        return {
            "target": target,
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
    """Выполнить полный аудит всех целей инфраструктуры и сохранить результат."""
    targets = get_monitored_targets(db)
    results = []

    async with httpx.AsyncClient(timeout=15.0, trust_env=False, headers={"User-Agent": "VPNBZK-Monitor/1.0"}) as client:
        # Проверяем цели параллельно
        tasks = [check_target(client, item["target"]) for item in targets]
        checked = await asyncio.gather(*tasks, return_exceptions=True)

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
                entry = {**item, **res}
            results.append(entry)

    blocked_count = sum(1 for r in results if r.get("blocked"))
    now_iso = datetime.now(timezone.utc).isoformat()

    summary = {
        "checked_at": now_iso,
        "total": len(results),
        "blocked_count": blocked_count,
        "has_blocked": blocked_count > 0,
        "results": results,
    }

    # Сохраняем в базу данных в AppSetting
    try:
        set_setting(db, "cheburcheck_last_audit", json.dumps(summary, ensure_ascii=False))
        db.commit()
    except Exception as e:
        logger.error("Не удалось сохранить результат Cheburcheck в AppSetting: %s", e)

    # Оповещение в Telegram при наличии блокировок или если включен notify
    if notify and blocked_count > 0:
        await notify_telegram_alert(summary)

    return summary


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
