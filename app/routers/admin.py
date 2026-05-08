"""Роуты админ-панели: пользователи, ключи, инвайты, платежи, настройки, диагностика."""

import asyncio
import json as _json
import logging
import os
import re
import shutil
import socket
import subprocess
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.auth import generate_csrf_token, generate_invite_key, hash_password, list_invite_keys, delete_invite_key
from app.bruteforce import admin_guard, api_guard, login_guard
from app.dependencies import _format_bytes, require_admin, templates, verify_csrf, _client_ip
from app.models import (
    AppSetting, AuditLog, DEFAULT_SETTINGS, Guide, Payment, Server,
    User, VPNKey, get_db, get_setting, set_setting,
)
from app.xray import (
    DOMAIN, GB, NL_SERVER_IP, REALITY_PORT, REALITY_PUBLIC_KEY,
    RU_SERVER_IP, XRAY_CONFIG_PATH, generate_uuid, get_user_links, sync_and_reload,
)

logger = logging.getLogger(__name__)
router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


def _log_action(db: Session, admin_id: int, action: str, target: str = "", detail: str = "") -> None:
    db.add(AuditLog(admin_id=admin_id, action=action, target=target, detail=detail))
    db.commit()


def _bg_sync_and_reload() -> None:
    """Фоновая синхронизация Xray — запускается после ответа клиенту."""
    try:
        from app.dependencies import get_db as _get_db
        db = next(_get_db())
        sync_and_reload(db)
    except Exception as e:
        logger.error("Ошибка фоновой синхронизации Xray: %s", e)


# ── Dashboard ──


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    all_users = db.query(User).order_by(User.id).all()
    all_keys = db.query(VPNKey).all()

    total_users = len(all_users)
    active_users = sum(1 for u in all_users if u.is_active)
    total_keys = len(all_keys)
    active_keys = sum(1 for k in all_keys if k.status == "active")
    expired_keys = sum(1 for k in all_keys if k.status == "expired")
    disabled_keys = sum(1 for k in all_keys if k.status == "disabled")
    limited_keys = sum(1 for k in all_keys if k.status == "limited")
    traffic_total = sum(k.data_used for k in all_keys)

    enriched_users = []
    for u in all_users:
        user_keys = [k for k in all_keys if k.user_id == u.id]
        enriched_users.append({
            "user": u,
            "keys": [{
                "id": k.id,
                "name": k.name,
                "uuid": k.uuid,
                "protocol": k.protocol,
                "is_active": k.is_active,
                "data_used": k.data_used or 0,
                "data_limit": k.data_limit,
                "expire_at": k.expire_at.isoformat() if k.expire_at else None,
                "status": k.status,
            } for k in user_keys],
            "keys_count": len(user_keys),
            "active_keys": sum(1 for k in user_keys if k.status == "active"),
            "traffic_used": sum(k.data_used or 0 for k in user_keys),
            "has_password": bool(u.password_hash),
        })

    # Audit logs (последние 50)
    audit_logs_raw = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(50).all()
    admin_ids = {log.admin_id for log in audit_logs_raw}
    admin_map = {}
    if admin_ids:
        for a in db.query(User).filter(User.id.in_(admin_ids)).all():
            admin_map[a.id] = a.display_name or f"tg:{a.telegram_id}"
    for log in audit_logs_raw:
        log.admin_name = admin_map.get(log.admin_id, f"ID:{log.admin_id}")

    xray_container = os.getenv("XRAY_CONTAINER_NAME", "ufobzk-xray")
    webapp_url = os.getenv("WEBAPP_URL", f"https://{DOMAIN}")
    from app.bot import TELEGRAM_BOT_USERNAME
    from app.models import SUPERADMIN_TELEGRAM_ID

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": admin,
            "enriched_users": enriched_users,
            "total_users": total_users,
            "active_users": active_users,
            "total_keys": total_keys,
            "active_keys": active_keys,
            "expired_keys": expired_keys,
            "disabled_keys": disabled_keys,
            "limited_keys": limited_keys,
            "traffic_total": traffic_total,
            "invite_keys": list_invite_keys(db),
            "csrf_token": generate_csrf_token(),
            "audit_logs": audit_logs_raw,
            "xray_config_path": XRAY_CONFIG_PATH,
            "xray_container": xray_container,
            "domain": DOMAIN,
            "nl_server_ip": NL_SERVER_IP,
            "ru_server_ip": RU_SERVER_IP,
            "reality_port": REALITY_PORT,
            "reality_active": bool(REALITY_PUBLIC_KEY),
            "superadmin_tg_id": SUPERADMIN_TELEGRAM_ID,
            "bot_username": TELEGRAM_BOT_USERNAME,
            "webapp_url": webapp_url,
            "admin_id": admin.id,
        },
    )


# ── Создание пользователя (form) ──


@router.post("/admin/users")
@limiter.limit("10/minute")
async def admin_create_user(
    request: Request,
    background_tasks: BackgroundTasks,
    display_name: str = Form(""),
    telegram_id: int = Form(0),
    username: str = Form(""),
    password: str = Form(""),
    data_limit_gb: float = Form(0),
    expire_days: int = Form(0),
    key_name: str = Form("default"),
    csrf_token: str = Form(""),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)

    from app.schemas import validate_create_user
    validated = validate_create_user(
        display_name=display_name, telegram_id=telegram_id,
        username=username, password=password,
        data_limit_gb=data_limit_gb, expire_days=expire_days,
        key_name=key_name,
    )

    from app.schemas import create_user_and_key
    new_user, vpn_key = create_user_and_key(db, validated)

    background_tasks.add_task(_bg_sync_and_reload)
    _log_action(db, admin.id, "create_user", str(validated.telegram_id), f"key={vpn_key.uuid}")
    return RedirectResponse(url="/admin", status_code=303)


# ── Создание пользователя (JSON API) ──


@router.post("/admin/api/create-user")
@limiter.limit("10/minute")
async def admin_create_user_json(
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    body = await request.json()
    csrf_token = str(body.get("csrf_token", ""))
    verify_csrf(request, csrf_token)

    from app.schemas import validate_create_user
    validated = validate_create_user(
        display_name=str(body.get("display_name", "")).strip(),
        telegram_id=int(body.get("telegram_id", 0) or 0),
        username=str(body.get("username", "")).strip(),
        password=str(body.get("password", "")).strip(),
        data_limit_gb=float(body.get("data_limit_gb", 0) or 0),
        expire_days=int(body.get("expire_days", 0) or 0),
        key_name=str(body.get("key_name", "default")).strip(),
    )

    from app.schemas import create_user_and_key
    new_user, vpn_key = create_user_and_key(db, validated)

    try:
        _log_action(db, admin.id, "create_user", str(validated.telegram_id), f"key={vpn_key.uuid}")
    except Exception as e:
        logger.error("Ошибка audit log: %s", e)

    # sync_and_reload запускаем в фоне — не блокируем ответ клиенту
    background_tasks.add_task(_bg_sync_and_reload)

    return JSONResponse({"ok": True, "user_id": new_user.id, "display_name": new_user.display_name, "username": new_user.username, "telegram_id": new_user.telegram_id})


@router.post("/admin/users/{user_id}/keys")
async def admin_add_key(
    user_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    body = await request.json()

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    key_name = str(body.get("name", "key")).strip()[:64]
    data_limit_gb = float(body.get("data_limit_gb", 0))
    expire_days = int(body.get("expire_days", 0))

    vpn_key = VPNKey(
        user_id=target.id,
        name=key_name,
        uuid=generate_uuid(),
        protocol="vless",
        data_limit=int(data_limit_gb * GB) if data_limit_gb > 0 else None,
        expire_at=datetime.now(timezone.utc) + timedelta(days=expire_days) if expire_days > 0 else None,
    )
    db.add(vpn_key)
    db.commit()

    background_tasks.add_task(_bg_sync_and_reload)
    _log_action(db, admin.id, "add_key", str(target.telegram_id), f"key={vpn_key.uuid} name={key_name}")
    return JSONResponse({"ok": True, "key_id": vpn_key.id, "uuid": vpn_key.uuid})


# ── Ротация всех ключей пользователя ──


@router.post("/admin/users/{user_id}/rotate-keys")
async def admin_rotate_keys(
    user_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    keys = db.query(VPNKey).filter(VPNKey.user_id == user_id).all()
    if not keys:
        raise HTTPException(status_code=400, detail="У пользователя нет ключей")

    old_uuids = [k.uuid for k in keys]
    for key in keys:
        key.uuid = generate_uuid()
    db.commit()

    background_tasks.add_task(_bg_sync_and_reload)
    _log_action(
        db, admin.id, "rotate_keys",
        str(target.telegram_id or target.id),
        f"count={len(keys)} old={','.join(old_uuids)}",
    )
    return JSONResponse({"ok": True, "rotated": len(keys)})


# ── Редактирование пользователя ──


@router.put("/admin/users/{user_id}")
async def admin_edit_user(
    user_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    body = await request.json()

    need_reload = False
    if "display_name" in body:
        target.display_name = str(body["display_name"]).strip()[:100] or None
    if "is_active" in body:
        target.is_active = bool(body["is_active"])
        need_reload = True
    if "is_admin" in body:
        target.is_admin = bool(body["is_admin"])
    if "username" in body:
        new_username = str(body["username"]).strip()[:64] or None
        if new_username and new_username != target.username:
            exists = db.query(User).filter(User.username == new_username, User.id != target.id).first()
            if exists:
                raise HTTPException(status_code=409, detail="Логин уже занят")
        target.username = new_username
    db.commit()

    if need_reload:
        background_tasks.add_task(_bg_sync_and_reload)

    _log_action(db, admin.id, "edit_user", str(target.telegram_id or target.username), str(body))
    return JSONResponse({"ok": True})


# ── Установка/смена пароля пользователя ──


@router.post("/admin/users/{user_id}/password")
async def admin_set_password(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    body = await request.json()
    new_password = str(body.get("password", "")).strip()
    if len(new_password) < 4:
        raise HTTPException(status_code=400, detail="Пароль должен быть не менее 4 символов")

    target.password_hash = hash_password(new_password)
    if not target.username:
        new_username = str(body.get("username", "")).strip()[:64]
        if new_username:
            exists = db.query(User).filter(User.username == new_username, User.id != target.id).first()
            if exists:
                raise HTTPException(status_code=409, detail="Логин уже занят")
            target.username = new_username
    db.commit()

    _log_action(db, admin.id, "set_password", str(target.telegram_id or target.username), f"user_id={target.id}")
    return JSONResponse({"ok": True})


# ── Редактирование ключа ──


@router.put("/admin/keys/{key_id}")
async def admin_edit_key(
    key_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    key = db.query(VPNKey).filter(VPNKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Ключ не найден")

    body = await request.json()

    if "name" in body and body["name"]:
        key.name = str(body["name"]).strip()[:64]
    if "is_active" in body:
        key.is_active = bool(body["is_active"])
    if "data_limit_gb" in body and body["data_limit_gb"] is not None:
        gb = float(body["data_limit_gb"])
        key.data_limit = int(gb * GB) if gb > 0 else None
    if "expire_days" in body and body["expire_days"] is not None:
        days = int(body["expire_days"])
        key.expire_at = datetime.now(timezone.utc) + timedelta(days=days) if days > 0 else None
    if "reset_traffic" in body and body["reset_traffic"]:
        key.data_used = 0

    db.commit()

    background_tasks.add_task(_bg_sync_and_reload)
    _log_action(db, admin.id, "edit_key", str(key.uuid), str(body))
    return JSONResponse({"ok": True})


# ── Удаление пользователя ──


@router.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    target_info = f"tg={target.telegram_id}"
    db.delete(target)  # cascade удалит и ключи
    db.commit()

    background_tasks.add_task(_bg_sync_and_reload)
    _log_action(db, admin.id, "delete_user", target_info)
    return JSONResponse({"ok": True})


# ── Удаление ключа ──


@router.delete("/admin/vpnkeys/{key_id}")
async def admin_delete_key(
    key_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    key = db.query(VPNKey).filter(VPNKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Ключ не найден")

    key_info = f"uuid={key.uuid} user_id={key.user_id}"
    db.delete(key)
    db.commit()

    background_tasks.add_task(_bg_sync_and_reload)
    _log_action(db, admin.id, "delete_vpnkey", key_info)
    return JSONResponse({"ok": True})


# ── Инвайт-ключи ──


@router.post("/admin/invite-keys")
async def admin_create_invite_key(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    invite = generate_invite_key(db, admin.id)
    _log_action(db, admin.id, "create_key", invite.key)
    return JSONResponse({"ok": True, "key": invite.key, "id": invite.id})


@router.get("/admin/invite-keys")
async def admin_list_invite_keys(
    request: Request,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    keys = list_invite_keys(db)
    return JSONResponse([
        {
            "id": k.id,
            "key": k.key,
            "is_used": k.is_used,
            "used_by": k.used_by,
            "created_at": k.created_at.strftime("%d.%m.%Y %H:%M") if k.created_at else None,
            "used_at": k.used_at.strftime("%d.%m.%Y %H:%M") if k.used_at else None,
        }
        for k in keys
    ])


@router.delete("/admin/invite-keys/{key_id}")
async def admin_delete_invite_key(
    key_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not delete_invite_key(db, key_id):
        raise HTTPException(status_code=404, detail="Ключ не найден")
    _log_action(db, admin.id, "delete_invite_key", str(key_id))
    return JSONResponse({"ok": True})


@router.post("/admin/invite-keys/bulk")
async def admin_create_bulk_invites(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    body = await request.json()
    count = min(int(body.get("count", 5)), 50)
    created = []
    for _ in range(count):
        invite = generate_invite_key(db, admin.id)
        created.append(invite.key)
    _log_action(db, admin.id, "create_bulk_invites", "", f"count={count}")
    return JSONResponse({"ok": True, "count": len(created), "keys": created})


# ── Синхронизация Xray вручную ──


@router.post("/admin/sync-xray")
async def admin_sync_xray(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        success = await asyncio.to_thread(sync_and_reload, db)
        _log_action(db, admin.id, "sync_xray", "", f"success={success}")
        return JSONResponse({"ok": success})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Brute-force управление ──


@router.get("/admin/security")
async def admin_security_stats(request: Request, _: User = Depends(require_admin), db: Session = Depends(get_db)):
    return JSONResponse({
        "login_guard": login_guard.get_stats(),
        "admin_guard": admin_guard.get_stats(),
        "api_guard": api_guard.get_stats(),
    })


@router.post("/admin/unban")
async def admin_unban_ip(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    body = await request.json()
    ip = body.get("ip", "").strip()
    if not ip:
        raise HTTPException(status_code=400, detail="IP не указан")
    results = {
        "login": login_guard.unban(ip),
        "admin": admin_guard.unban(ip),
        "api": api_guard.unban(ip),
    }
    _log_action(db, admin.id, "unban_ip", ip, str(results))
    return JSONResponse({"ok": True, "unbanned": results})


# ── Журнал аудита (JSON) ──


@router.get("/admin/audit-log")
async def admin_audit_log(
    request: Request,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    logs = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(200).all()
    admin_ids = {log.admin_id for log in logs}
    admin_map: dict[int, str] = {}
    if admin_ids:
        for a in db.query(User).filter(User.id.in_(admin_ids)).all():
            admin_map[a.id] = a.display_name or f"tg:{a.telegram_id}"
    return JSONResponse([
        {
            "id": log.id,
            "admin_id": log.admin_id,
            "admin_name": admin_map.get(log.admin_id, f"ID:{log.admin_id}"),
            "action": log.action,
            "target": log.target,
            "detail": log.detail,
            "created_at": log.created_at.strftime("%d.%m.%Y %H:%M") if log.created_at else None,
        }
        for log in logs
    ])


# ── VPN-ссылки пользователя (для админки) ──


@router.get("/admin/users/{user_id}/keys")
async def admin_list_user_keys(
    user_id: int,
    request: Request,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    keys = [{
        "id": k.id,
        "name": k.name,
        "uuid": k.uuid,
        "protocol": k.protocol,
        "is_active": k.is_active,
        "data_used": k.data_used or 0,
        "data_limit": k.data_limit,
        "expire_at": k.expire_at.isoformat() if k.expire_at else None,
        "status": k.status,
    } for k in target.vpn_keys]
    return JSONResponse({"keys": keys})


@router.get("/admin/users/{user_id}/links")
async def admin_user_links(
    user_id: int,
    request: Request,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    active_keys = [k for k in target.vpn_keys if k.status == "active"]
    links = []
    for key in active_keys:
        key_links = get_user_links(key)
        for link_info in key_links:
            links.append({
                "key_name": key.name,
                "link_name": link_info["name"],
                "link": link_info["link"],
            })

    webapp_url = os.getenv("WEBAPP_URL", f"https://{DOMAIN}")
    sub_token = target.sub_token or ""
    subscription_url = f"{webapp_url}/sub/{sub_token}" if active_keys and sub_token else ""

    return JSONResponse({"links": links, "subscription_url": subscription_url})


# ── Платежи / биллинг ──


@router.get("/admin/payments")
async def admin_list_payments(
    request: Request,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    payments = db.query(Payment).order_by(Payment.id.desc()).limit(500).all()
    users = {u.id: u for u in db.query(User).all()}
    result = []
    for p in payments:
        u = users.get(p.user_id)
        result.append({
            "id": p.id,
            "user_id": p.user_id,
            "user_name": u.display_name or u.username or f"ID:{p.user_id}" if u else f"ID:{p.user_id}",
            "amount": p.amount,
            "currency": p.currency,
            "status": p.status,
            "note": p.note or "",
            "period_start": p.period_start.strftime("%d.%m.%Y") if p.period_start else None,
            "period_end": p.period_end.strftime("%d.%m.%Y") if p.period_end else None,
            "created_at": p.created_at.strftime("%d.%m.%Y %H:%M") if p.created_at else None,
        })
    return JSONResponse(result)


@router.get("/admin/payments/summary")
async def admin_payments_summary(
    request: Request,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    users = db.query(User).filter(User.is_active == True).all()  # noqa: E712
    summary = []
    for u in users:
        payments = [p for p in u.payments]
        paid_total = sum(p.amount for p in payments if p.status == "paid")
        debt_total = sum(p.amount for p in payments if p.status == "debt")
        last_paid = max((p.created_at for p in payments if p.status == "paid"), default=None)

        # Last 3 paid months
        paid_months = []
        for p in sorted(
            [p for p in payments if p.status == "paid"],
            key=lambda x: x.period_start or x.created_at,
            reverse=True,
        )[:3]:
            dt = p.period_start or p.created_at
            paid_months.append(dt.strftime("%m.%Y") if dt else "—")

        # Current month paid?
        current_paid = any(
            p.status == "paid"
            and p.period_start
            and p.period_start.year == now.year
            and p.period_start.month == now.month
            for p in payments
        )

        summary.append({
            "user_id": u.id,
            "user_name": u.display_name or u.username or f"tg:{u.telegram_id}",
            "is_free": u.is_free,
            "free_reason": u.free_reason,
            "keys_count": len(u.vpn_keys),
            "active_keys": sum(1 for k in u.vpn_keys if k.status == "active"),
            "paid_total": paid_total,
            "debt_total": debt_total,
            "balance": paid_total - debt_total,
            "has_debt": debt_total > 0,
            "last_paid": last_paid.strftime("%d.%m.%Y %H:%M") if last_paid else None,
            "paid_months": paid_months,
            "current_month_paid": current_paid,
        })
    summary.sort(key=lambda x: (-x["has_debt"], x["user_name"]))
    return JSONResponse(summary)


@router.post("/admin/payments")
async def admin_create_payment(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    body = await request.json()
    user_id = int(body.get("user_id", 0))
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id обязателен")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    amount = float(body.get("amount", 0))
    status = body.get("status", "paid")
    if status not in ("paid", "debt", "pending"):
        raise HTTPException(status_code=400, detail="Недопустимый статус")
    currency = str(body.get("currency", "RUB"))[:8]
    note = str(body.get("note", ""))[:256]

    period_start_raw = body.get("period_start")
    period_end_raw = body.get("period_end")

    def _parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None

    payment = Payment(
        user_id=user_id,
        amount=amount,
        currency=currency,
        status=status,
        note=note,
        period_start=_parse_date(period_start_raw),
        period_end=_parse_date(period_end_raw),
        created_by=admin.id,
    )
    db.add(payment)
    db.commit()

    _log_action(db, admin.id, "add_payment", str(user_id), f"status={status} amount={amount} note={note}")
    return JSONResponse({"ok": True, "id": payment.id})


@router.delete("/admin/payments/{payment_id}")
async def admin_delete_payment(
    payment_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    p = db.query(Payment).filter(Payment.id == payment_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Запись не найдена")
    db.delete(p)
    db.commit()
    _log_action(db, admin.id, "delete_payment", str(payment_id))
    return JSONResponse({"ok": True})


@router.post("/admin/payments/quick-month")
async def admin_quick_month_payment(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Быстрое добавление оплаты за текущий/прошлый месяц."""
    body = await request.json()
    user_id = int(body.get("user_id", 0))
    amount = float(body.get("amount", 0))
    month_offset = int(body.get("month_offset", 0))  # 0=текущий, -1=прошлый
    note = str(body.get("note", "")).strip()

    if not user_id or amount <= 0:
        raise HTTPException(status_code=400, detail="user_id и amount обязательны")

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    # Вычисляем начало месяца с учётом offset
    import calendar
    year = now.year
    month = now.month + month_offset
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1

    start = datetime(year, month, 1, tzinfo=timezone.utc)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)

    payment = Payment(
        user_id=user_id,
        amount=amount,
        currency="RUB",
        status="paid",
        period_start=start,
        period_end=end,
        note=note or f"Оплата {start.strftime('%B %Y')}",
        created_by=admin.id,
    )
    db.add(payment)
    db.commit()
    _log_action(db, admin.id, "quick_month_pay", str(user_id), f"{amount}₽ {start.strftime('%m.%Y')}")
    return JSONResponse({"ok": True, "id": payment.id})


@router.post("/admin/users/{user_id}/toggle-free")
async def admin_toggle_free(
    user_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Переключить бесплатный доступ для пользователя (навсегда)."""
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    body = await request.json()
    is_free = bool(body.get("is_free", not target.is_free))
    reason = str(body.get("reason", "")).strip()

    target.is_free = is_free
    if reason:
        target.free_reason = reason

    # Убираем лимиты со всех ключей при включении free
    if is_free:
        for key in target.vpn_keys:
            key.data_limit = None
            key.expire_at = None
            key.is_active = True

    db.commit()
    action = "free_on" if is_free else "free_off"
    _log_action(db, admin.id, action, str(user_id), reason or "")
    return JSONResponse({"ok": True, "is_free": is_free})


@router.get("/admin/users/{user_id}/status")
async def admin_user_status(
    user_id: int,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Полный статус пользователя для billing таба."""
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    payments = target.payments
    paid_total = sum(p.amount for p in payments if p.status == "paid")
    debt_total = sum(p.amount for p in payments if p.status == "debt")

    # Последние 3 оплаченных месяца
    paid_months = []
    for p in sorted([p for p in payments if p.status == "paid"], key=lambda x: x.period_start or x.created_at, reverse=True)[:3]:
        month_str = (p.period_start.strftime("%m.%Y") if p.period_start else p.created_at.strftime("%m.%Y")) if p.created_at else "—"
        paid_months.append(month_str)

    # Текущий месяц оплачен?
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    current_paid = any(
        p.status == "paid" and p.period_start and p.period_start.year == now.year and p.period_start.month == now.month
        for p in payments
    )

    return JSONResponse({
        "user_id": target.id,
        "name": target.display_name or target.username or f"tg:{target.telegram_id}",
        "is_free": target.is_free,
        "free_reason": target.free_reason,
        "is_active": target.is_active,
        "paid_total": paid_total,
        "debt_total": debt_total,
        "balance": paid_total - debt_total,
        "paid_months": paid_months,
        "current_month_paid": current_paid,
        "keys_count": len(target.vpn_keys),
        "active_keys": sum(1 for k in target.vpn_keys if k.status == "active"),
    })


# ── REALITY auto-generation ──

@router.post("/admin/api/generate-reality-keys")
async def admin_generate_reality_keys(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Автогенерация REALITY ключей (private + public + shortId)."""
    try:
        import subprocess
        result = subprocess.run(
            ["xray", "x25519"],
            capture_output=True, text=True, timeout=10, check=True
        )
        lines = result.stdout.strip().split("\n")
        private_key = ""
        public_key = ""
        for line in lines:
            if "Private key:" in line:
                private_key = line.split(":")[-1].strip()
            if "Public key:" in line:
                public_key = line.split(":")[-1].strip()

        if not private_key or not public_key:
            raise HTTPException(status_code=500, detail="Не удалось распарсить ключи из xray x25519")

        import secrets
        short_id = secrets.token_hex(4)  # 8 hex chars

        _log_action(db, admin.id, "gen_reality", "", f"pub={public_key[:16]}... sid={short_id}")
        return JSONResponse({
            "ok": True,
            "private_key": private_key,
            "public_key": public_key,
            "short_id": short_id,
        })
    except FileNotFoundError:
        # Fallback для серверов без xray CLI
        import secrets
        # Генерируем корректные base64-encoded x25519 ключи через cryptography
        try:
            import base64
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
            from cryptography.hazmat.primitives import serialization
            priv = X25519PrivateKey.generate()
            pub = priv.public_key()
            private_key = base64.b64encode(priv.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption()
            )).decode('ascii')
            public_key = base64.b64encode(pub.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw
            )).decode('ascii')
            short_id = secrets.token_hex(4)
            _log_action(db, admin.id, "gen_reality", "", f"pub={public_key[:16]}... sid={short_id}")
            return JSONResponse({
                "ok": True,
                "private_key": private_key,
                "public_key": public_key,
                "short_id": short_id,
            })
        except ImportError:
            raise HTTPException(status_code=500, detail="xray не найден и cryptography не установлен. Установите: pip install cryptography")
    except Exception as e:
        logger.error("Ошибка генерации REALITY ключей: %s", e)
        raise HTTPException(status_code=500, detail=f"Ошибка генерации: {str(e)}")


# ── Servers management ──

@router.get("/admin/api/servers")
async def admin_list_servers(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Список удалённых серверов (xray-nodes)."""
    servers = db.query(Server).order_by(Server.priority.asc()).all()
    return JSONResponse([{
        "id": s.id,
        "name": s.name,
        "region": s.region,
        "host": s.host,
        "api_url": s.api_url,
        "ws_port": s.ws_port,
        "reality_port": s.reality_port,
        "grpc_port": s.grpc_port,
        "xhttp_port": s.xhttp_port,
        "priority": s.priority,
        "role": s.role,
        "reality_public_key": s.reality_public_key,
        "reality_short_id": s.reality_short_id,
        "last_sync": s.last_sync.isoformat() if s.last_sync else None,
        "last_sync_status": s.last_sync_status,
        "created_at": s.created_at.isoformat() if s.created_at else None,
    } for s in servers])


@router.post("/admin/api/servers")
async def admin_add_server(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Добавить новый удалённый сервер."""
    body = await request.json()

    if not body.get("name") or not body.get("host") or not body.get("api_url") or not body.get("api_token"):
        raise HTTPException(status_code=400, detail="name, host, api_url и api_token обязательны")

    # Проверим дубли host
    existing = db.query(Server).filter(Server.host == body["host"]).first()
    if existing:
        raise HTTPException(status_code=409, detail="Сервер с таким host уже существует")

    server = Server(
        name=body["name"],
        region=body.get("region", "eu"),
        host=body["host"],
        api_url=body["api_url"],
        api_token=body["api_token"],
        ws_port=body.get("ws_port", 443),
        reality_port=body.get("reality_port", 2053),
        grpc_port=body.get("grpc_port", 8445),
        xhttp_port=body.get("xhttp_port", 8444),
        priority=body.get("priority", 100),
        role=body.get("role", "remote"),
        reality_public_key=body.get("reality_public_key") or None,
        reality_private_key=body.get("reality_private_key") or None,
        reality_short_id=body.get("reality_short_id") or None,
        reality_dest=body.get("reality_dest") or None,
        reality_server_names=body.get("reality_server_names") or None,
    )
    db.add(server)
    db.commit()
    _log_action(db, admin.id, "add_server", str(server.id), server.name)
    return JSONResponse({"ok": True, "id": server.id})


@router.post("/admin/api/servers/{server_id}/delete")
async def admin_delete_server(
    server_id: int,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Удалить сервер (не удаляя ключи пользователей, т.к. они могут быть перенаправлены)."""
    server = db.query(Server).filter(Server.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")

    db.delete(server)
    db.commit()
    _log_action(db, None, "delete_server", str(server_id), server.name)
    return JSONResponse({"ok": True})


@router.post("/admin/api/servers/{server_id}/sync")
async def admin_sync_server(
    server_id: int,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Синхронизировать один сервер через xray-node API."""
    from datetime import datetime, timezone
    from app.remote_xray import sync_server as remote_sync

    server = db.query(Server).filter(Server.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")

    try:
        await remote_sync(db, server)
        server.last_sync = datetime.now(timezone.utc)
        server.last_sync_status = "ok"
        db.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        server.last_sync = datetime.now(timezone.utc)
        server.last_sync_status = "error"
        db.commit()
        logger.error("Ошибка синхронизации сервера %s: %s", server.name, e)
        raise HTTPException(status_code=502, detail=f"Ошибка синхронизации: {str(e)}")


@router.get("/admin/api/servers/check-all")
async def admin_check_all_servers(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Проверить доступность (health + TCP REALITY) всех серверов."""
    import asyncio
    import socket as _socket
    import time as _t
    import httpx as _httpx

    servers = db.query(Server).order_by(Server.priority.asc()).all()

    async def _check(s: Server) -> dict:
        res: dict[str, Any] = {
            "id": s.id,
            "name": s.name,
            "host": s.host,
            "health_ok": False,
            "health_ms": None,
            "reality_ok": False,
            "reality_ms": None,
            "error": None,
        }
        # 1. HTTP health check xray-node
        try:
            t0 = _t.monotonic()
            async with _httpx.AsyncClient(timeout=5.0, verify=False) as cl:
                resp = await cl.get(f"{s.api_url.rstrip('/')}/health")
            res["health_ms"] = round((_t.monotonic() - t0) * 1000)
            res["health_ok"] = resp.status_code == 200
        except Exception as e:
            res["error"] = str(e)

        # 2. TCP probe to REALITY port
        if s.reality_port:
            try:
                t0 = _t.monotonic()
                await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda h=s.host, p=s.reality_port: _socket.create_connection((h, p), timeout=3).close()
                    ),
                    timeout=4.0
                )
                res["reality_ms"] = round((_t.monotonic() - t0) * 1000)
                res["reality_ok"] = True
            except Exception:
                res["reality_ok"] = False

        return res

    results = await asyncio.gather(*[_check(s) for s in servers])
    return JSONResponse(list(results))


@router.post("/admin/api/servers/sync-all")
async def admin_sync_all_servers(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Синхронизировать все сервера."""
    from datetime import datetime, timezone
    from app.remote_xray import sync_server as remote_sync
    import asyncio

    servers = db.query(Server).all()
    results = {}
    for s in servers:
        try:
            await remote_sync(db, s)
            s.last_sync = datetime.now(timezone.utc)
            s.last_sync_status = "ok"
            results[s.id] = True
        except Exception as e:
            s.last_sync = datetime.now(timezone.utc)
            s.last_sync_status = "error"
            results[s.id] = False
            logger.error("Ошибка синхронизации сервера %s: %s", s.name, e)
    db.commit()
    return JSONResponse({"ok": True, "results": results})


# ── Guides ──

@router.get("/admin/api/guides")
async def admin_list_guides(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Список всех гайдов."""
    guides = db.query(Guide).order_by(Guide.sort_order.asc(), Guide.id.asc()).all()
    return JSONResponse([{
        "id": g.id,
        "slug": g.slug,
        "title": g.title,
        "app_name": g.app_name,
        "platform": g.platform,
        "content": g.content,
        "sort_order": g.sort_order,
        "is_active": g.is_active,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
    } for g in guides])


@router.post("/admin/api/guides")
async def admin_create_guide(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Создать новый гайд."""
    body = await request.json()
    slug = str(body.get("slug", "")).strip()
    title = str(body.get("title", "")).strip()
    if not slug or not title:
        raise HTTPException(status_code=400, detail="slug и title обязательны")
    if db.query(Guide).filter(Guide.slug == slug).first():
        raise HTTPException(status_code=409, detail="Гайд с таким slug уже существует")

    guide = Guide(
        slug=slug,
        title=title,
        app_name=str(body.get("app_name", "")).strip() or title,
        platform=str(body.get("platform", "all")).strip(),
        content=str(body.get("content", "")),
        sort_order=int(body.get("sort_order", 0)),
        is_active=bool(body.get("is_active", True)),
    )
    db.add(guide)
    db.commit()
    _log_action(db, admin.id, "create_guide", str(guide.id), guide.slug)
    return JSONResponse({"ok": True, "id": guide.id})


@router.put("/admin/api/guides/{guide_id}")
async def admin_update_guide(
    guide_id: int,
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Обновить гайд."""
    guide = db.query(Guide).filter(Guide.id == guide_id).first()
    if not guide:
        raise HTTPException(status_code=404, detail="Гайд не найден")
    body = await request.json()
    if "slug" in body:
        new_slug = str(body["slug"]).strip()
        if new_slug and new_slug != guide.slug:
            if db.query(Guide).filter(Guide.slug == new_slug).first():
                raise HTTPException(status_code=409, detail="Гайд с таким slug уже существует")
            guide.slug = new_slug
    if "title" in body:
        guide.title = str(body["title"]).strip()
    if "app_name" in body:
        guide.app_name = str(body["app_name"]).strip()
    if "platform" in body:
        guide.platform = str(body["platform"]).strip()
    if "content" in body:
        guide.content = str(body["content"])
    if "sort_order" in body:
        guide.sort_order = int(body["sort_order"])
    if "is_active" in body:
        guide.is_active = bool(body["is_active"])
    db.commit()
    _log_action(db, admin.id, "update_guide", str(guide_id), guide.slug)
    return JSONResponse({"ok": True})


@router.post("/admin/api/guides/{guide_id}/delete")
async def admin_delete_guide(
    guide_id: int,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Удалить гайд."""
    guide = db.query(Guide).filter(Guide.id == guide_id).first()
    if not guide:
        raise HTTPException(status_code=404, detail="Гайд не найден")
    db.delete(guide)
    db.commit()
    _log_action(db, None, "delete_guide", str(guide_id), guide.slug)
    return JSONResponse({"ok": True})


# ── Настройки приложения ──


@router.get("/admin/settings")
async def admin_get_settings(
    request: Request,
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.query(AppSetting).all()
    data = {r.key: r.value for r in rows}
    for k, v in DEFAULT_SETTINGS.items():
        if k not in data:
            data[k] = v
    return JSONResponse(data)


@router.put("/admin/settings")
async def admin_save_settings(
    request: Request,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    body = await request.json()
    allowed_keys = {
        "instructions_ios", "instructions_android",
        "instructions_windows", "instructions_macos",
        "app_link_ios", "app_link_android",
        "app_link_windows", "app_link_macos",
        "support_text",
    }
    saved = []
    for k, v in body.items():
        if k in allowed_keys:
            set_setting(db, k, str(v)[:4000])
            saved.append(k)
    _log_action(db, admin.id, "save_settings", "", f"keys={saved}")
    return JSONResponse({"ok": True, "saved": saved})


# ── Миграция NL сервера ──


@router.post("/admin/migrate-nl")
async def admin_migrate_nl(
    request: Request,
    background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Обновляет переменные окружения NL сервера в .env и перестраивает Xray-конфиг."""
    body = await request.json()

    env_path = ".env"
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        content = ""

    def upsert_env(text: str, key: str, val: str) -> str:
        pattern = rf"^{re.escape(key)}=.*$"
        new_line = f"{key}={val}"
        if re.search(pattern, text, re.MULTILINE):
            return re.sub(pattern, new_line, text, flags=re.MULTILINE)
        return text.rstrip("\n") + f"\n{new_line}\n"

    changes = {}
    for field in ("NL_SERVER_IP", "REALITY_PUBLIC_KEY", "REALITY_PRIVATE_KEY",
                  "REALITY_SHORT_ID", "RU_TRANSIT_UUID", "RU_TRANSIT_PUBLIC_KEY",
                  "RU_TRANSIT_SHORT_ID", "RU_TRANSIT_PORT", "RU_TRANSIT_SN"):
        if field in body and str(body[field]).strip():
            content = upsert_env(content, field, str(body[field]).strip())
            changes[field] = str(body[field]).strip()

    if not changes:
        raise HTTPException(status_code=400, detail="Нет данных для обновления")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    try:
        shutil.copy(env_path, f".env.backup.{ts}")
    except Exception:
        pass

    with open(env_path, "w", encoding="utf-8") as f:
        f.write(content)

    for k, v in changes.items():
        os.environ[k] = v

    background_tasks.add_task(_bg_sync_and_reload)
    _log_action(db, admin.id, "migrate_nl", "", f"changed={list(changes.keys())}")
    return JSONResponse({"ok": True, "changed": list(changes.keys())})


# ── Диагностика сервера ──


@router.get("/admin/diagnostics")
async def admin_diagnostics(
    request: Request,
    _: User = Depends(require_admin),
):
    results: dict[str, Any] = {}

    # ── 1. Системные ресурсы ──
    try:
        load = os.getloadavg()
        results["load_avg"] = {"1m": round(load[0], 2), "5m": round(load[1], 2), "15m": round(load[2], 2)}
    except Exception:
        results["load_avg"] = None

    try:
        mem: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mem[parts[0].rstrip(":")] = int(parts[1])
        total_kb = mem.get("MemTotal", 0)
        avail_kb = mem.get("MemAvailable", 0)
        used_kb = total_kb - avail_kb
        results["memory"] = {
            "total_mb": round(total_kb / 1024),
            "used_mb": round(used_kb / 1024),
            "free_mb": round(avail_kb / 1024),
            "pct": round(used_kb / total_kb * 100) if total_kb else 0,
        }
    except Exception:
        results["memory"] = None

    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        results["disk"] = {
            "total_gb": round(total / 1e9, 1),
            "used_gb": round(used / 1e9, 1),
            "free_gb": round(free / 1e9, 1),
            "pct": round(used / total * 100) if total else 0,
        }
    except Exception:
        results["disk"] = None

    try:
        with open("/proc/uptime") as f:
            uptime_secs = float(f.read().split()[0])
        days = int(uptime_secs // 86400)
        hours = int((uptime_secs % 86400) // 3600)
        minutes = int((uptime_secs % 3600) // 60)
        results["uptime"] = f"{days}д {hours}ч {minutes}м"
    except Exception:
        results["uptime"] = None

    # ── 2. TCP-connectivity тесты ──
    def tcp_check(host: str, port: int, timeout: float = 3.0) -> dict:
        t0 = _time.monotonic()
        try:
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.close()
            ms = round((_time.monotonic() - t0) * 1000)
            return {"ok": True, "ms": ms}
        except Exception as e:
            ms = round((_time.monotonic() - t0) * 1000)
            return {"ok": False, "ms": ms, "error": str(e)[:80]}

    results["xray_ws"] = tcp_check("xray", 8443)
    results["xray_xhttp"] = tcp_check("xray", 8444)
    results["reality"] = tcp_check("127.0.0.1", 443, timeout=3.0)
    results["app_self"] = tcp_check("127.0.0.1", 8000)

    # ── 3. DNS resolution ──
    def dns_check(host: str) -> dict:
        t0 = _time.monotonic()
        try:
            ip = socket.gethostbyname(host)
            ms = round((_time.monotonic() - t0) * 1000)
            return {"ok": True, "ip": ip, "ms": ms}
        except Exception as e:
            return {"ok": False, "error": str(e)[:80]}

    domain = os.getenv("DOMAIN", "")
    results["dns_domain"] = dns_check(domain) if domain else {"ok": False, "error": "DOMAIN not set"}
    results["dns_cloudflare"] = dns_check("one.one.one.one")
    results["dns_google"] = dns_check("google.com")

    # ── 4. BBR status ──
    try:
        with open("/proc/sys/net/ipv4/tcp_congestion_control") as f:
            algo = f.read().strip()
        results["bbr"] = {"active": algo == "bbr", "algorithm": algo}
    except Exception:
        results["bbr"] = {"active": False, "algorithm": "unknown"}

    # ── 5. Active TCP connections ──
    try:
        res = subprocess.run(
            ["sh", "-c", "ss -tn state established | wc -l"],
            capture_output=True, text=True, timeout=3
        )
        conn_count = int(res.stdout.strip()) - 1
        results["connections"] = {"established": max(conn_count, 0)}
    except Exception:
        results["connections"] = None

    # ── 6. Xray config file ──
    config_path = os.getenv("XRAY_CONFIG_PATH", "/etc/xray/config.json")
    try:
        stat = os.stat(config_path)
        with open(config_path) as f:
            cfg = _json.load(f)
        inbound_tags = [ib.get("tag", "?") for ib in cfg.get("inbounds", [])]
        client_count = 0
        for ib in cfg.get("inbounds", []):
            clients = ib.get("settings", {}).get("clients", [])
            client_count = max(client_count, len(clients))
        results["xray_config"] = {
            "ok": True,
            "size_kb": round(stat.st_size / 1024, 1),
            "inbounds": inbound_tags,
            "clients": client_count,
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M"),
        }
    except FileNotFoundError:
        results["xray_config"] = {"ok": False, "error": "Файл не найден (Xray ещё не запущен?)"}
    except Exception as e:
        results["xray_config"] = {"ok": False, "error": str(e)[:100]}

    # ── 7. Certificate expiry ──
    try:
        cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"
        res = subprocess.run(
            ["openssl", "x509", "-noout", "-enddate", "-in", cert_path],
            capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0:
            date_str = res.stdout.strip().replace("notAfter=", "")
            from email.utils import parsedate
            import calendar
            t = parsedate(date_str)
            if t:
                exp_ts = calendar.timegm(t)
                days_left = int((exp_ts - _time.time()) / 86400)
                results["cert"] = {
                    "ok": days_left > 7,
                    "expires": date_str,
                    "days_left": days_left,
                }
            else:
                results["cert"] = {"ok": True, "expires": date_str, "days_left": None}
        else:
            results["cert"] = {"ok": False, "error": res.stderr.strip()[:100]}
    except Exception as e:
        results["cert"] = {"ok": False, "error": str(e)[:100]}

    return JSONResponse(results)

