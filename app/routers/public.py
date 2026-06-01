"""Публичные роуты: главная, логин, логаут, подписки, health."""

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.auth import (
    SESSION_COOKIE,
    create_session_token,
    generate_csrf_token,
    verify_admin_password,
    verify_code_and_get_user,
    verify_user_credentials,
)
from app.bot import TELEGRAM_BOT_USERNAME
from app.bruteforce import login_guard
from app.dependencies import _client_ip, _get_current_user, templates, verify_csrf
from app.models import Guide, SUPERADMIN_TELEGRAM_ID, User, get_db
from app.xray import get_subscription_content, get_subscription_json

logger = logging.getLogger(__name__)
router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


@router.get("/health")
async def health(db: Session = Depends(get_db)):
    result: dict = {"status": "ok", "db": "ok"}
    try:
        db.execute(sa_text("SELECT 1"))
    except Exception:
        result["db"] = "error"
        result["status"] = "degraded"
    code = 200 if result["status"] == "ok" else 503
    return JSONResponse(result, status_code=code)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "bot_username": TELEGRAM_BOT_USERNAME,
            "csrf_token": generate_csrf_token(),
        },
    )


@router.post("/login")
@limiter.limit("5/minute")
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    code: str = Form(""),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    ip = _client_ip(request)
    if login_guard.is_blocked(ip):
        remaining = login_guard.get_ban_remaining(ip)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": f"Слишком много попыток. Повторите через {remaining // 60 + 1} мин.",
                "bot_username": TELEGRAM_BOT_USERNAME,
                "csrf_token": generate_csrf_token(),
            },
            status_code=429,
        )

    verify_csrf(request, csrf_token)

    user = None

    # ── 1. Логин + пароль пользователя ──
    if username.strip() and password.strip():
        user = verify_user_credentials(db, username.strip(), password.strip())

    # ── 2. Пароль администратора (env ADMIN_PASSWORD) ──
    if user is None and code.strip() and verify_admin_password(code.strip()):
        user = db.query(User).filter(
            User.telegram_id == SUPERADMIN_TELEGRAM_ID,
            User.is_active == True,  # noqa: E712
        ).first() if SUPERADMIN_TELEGRAM_ID else None
        if user is None:
            user = db.query(User).filter(
                User.is_admin == True,  # noqa: E712
                User.is_active == True,  # noqa: E712
            ).first()
        if user is None:
            logger.warning("Верный ADMIN_PASSWORD, но admin-пользователь не найден в БД (IP=%s)", ip)

    # ── 3. Одноразовый Telegram-код ──
    if user is None and code.strip():
        user = verify_code_and_get_user(db, code.strip())

    if user is None:
        login_guard.record_failure(ip)
        logger.warning("Неудачный вход: IP=%s", ip)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Неверный логин/пароль или код.",
                "bot_username": TELEGRAM_BOT_USERNAME,
                "csrf_token": generate_csrf_token(),
            },
        )

    login_guard.record_success(ip)
    redirect_url = "/admin" if user.is_admin else "/cabinet"
    response = RedirectResponse(url=redirect_url, status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        create_session_token(user.id),
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=86400,
    )
    return response


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


def _sub_userinfo(keys) -> str:
    """Строка Subscription-Userinfo с upload/download/total/expire.

    expire — ближайший срок истечения среди ключей (UNIX timestamp).
    Hiddify, Clash, Nekoray отображают эти данные пользователю.
    """
    import calendar
    download = sum(k.data_used or 0 for k in keys)
    total = sum(k.data_limit or 0 for k in keys)
    expire_times = [
        calendar.timegm(k.expire_at.timetuple())
        for k in keys if k.expire_at
    ]
    expire_str = f"; expire={min(expire_times)}" if expire_times else ""
    return f"upload=0; download={download}; total={total}{expire_str}"


@router.get("/sub/{token}")
async def subscription(token: str, request: Request, db: Session = Depends(get_db)):
    """Эндпоинт подписки — возвращает base64 список ссылок (все серверы)."""
    user = db.query(User).filter(User.sub_token == token, User.is_active == True).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=404)
    keys = [k for k in user.vpn_keys if k.status == "active"]
    if not keys:
        raise HTTPException(status_code=404)
    content = get_subscription_content(keys, db=db)
    return PlainTextResponse(content, headers={
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": "inline",
        "Profile-Title": "VPNBZK",
        "Profile-Update-Interval": "12",
        "Subscription-Userinfo": _sub_userinfo(keys),
    })


@router.get("/sub/json/{token}")
async def subscription_json(token: str, db: Session = Depends(get_db)):
    """JSON подписка для v2rayNG/Hiddify/Nekoray — mux + fingerprint + multi-server."""
    user = db.query(User).filter(User.sub_token == token, User.is_active == True).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=404)
    keys = [k for k in user.vpn_keys if k.status == "active"]
    if not keys:
        raise HTTPException(status_code=404)
    result = get_subscription_json(keys, db=db)
    return JSONResponse(result)


@router.get("/sub/key/{key_uuid}")
async def subscription_key(key_uuid: str, request: Request, db: Session = Depends(get_db)):
    """Подписка для одного ключа — для шаринга с конкретным человеком."""
    from app.models import VPNKey
    key = db.query(VPNKey).filter(VPNKey.uuid == key_uuid).first()
    if not key:
        raise HTTPException(status_code=404)
    user = db.query(User).filter(User.id == key.user_id, User.is_active == True).first()  # noqa: E712
    if not user or key.status != "active":
        raise HTTPException(status_code=404)
    # db передаётся, чтобы включить ссылки со всех remote-серверов
    content = get_subscription_content([key], db=db)
    return PlainTextResponse(content, headers={
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": "inline",
        "Profile-Title": "VPNBZK",
        "Profile-Update-Interval": "12",
        "Subscription-Userinfo": _sub_userinfo([key]),
    })


@router.get("/api/settings")
async def api_settings(request: Request, db: Session = Depends(get_db)):
    """Возвращает только публичные настройки (инструкции, ссылки)."""
    from app.models import get_setting
    keys = [
        "instructions_ios", "instructions_android",
        "instructions_windows", "instructions_macos",
        "app_link_ios", "app_link_android",
        "app_link_windows", "app_link_macos",
        "support_text",
    ]
    return JSONResponse({k: get_setting(db, k) for k in keys})


@router.get("/api/guides")
async def api_guides(db: Session = Depends(get_db)):
    """Возвращает активные гайды для личного кабинета."""
    guides = db.query(Guide).filter(Guide.is_active == True).order_by(Guide.sort_order.asc(), Guide.id.asc()).all()  # noqa: E712
    return JSONResponse([{
        "id": g.id,
        "slug": g.slug,
        "title": g.title,
        "app_name": g.app_name,
        "platform": g.platform,
        "content": g.content,
    } for g in guides])
