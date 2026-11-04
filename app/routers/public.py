"""Публичные роуты: главная, логин, логаут, подписки, health."""

import logging
from datetime import datetime, timezone

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
    load_magic_token,
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


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    """Запретить всем поисковикам индексировать любые страницы."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n", media_type="text/plain")


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


@router.get("/login/magic")
async def login_magic(token: str, db: Session = Depends(get_db)):
    """Вход по одноразовой подписанной ссылке из Telegram без ручного ввода кода."""
    user_id = load_magic_token(token)
    if not user_id:
        raise HTTPException(
            status_code=400,
            detail="Ссылка для входа устарела или недействительна. Запросите новую в Telegram-боте.",
        )
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден или заблокирован.")

    user.last_login = datetime.now(timezone.utc)
    db.commit()

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
    # Если лимит не задан хотя бы для одного ключа (безлимит), выставляем большой запас (10 ТБ),
    # чтобы клиенты (Hiddify/Clash) не показывали ложное предупреждение «Лимит исчерпан» при total=0.
    has_unlimited = any(k.data_limit is None or k.data_limit <= 0 for k in keys)
    if has_unlimited:
        total = 10 * 1024 * 1024 * 1024 * 1024  # 10 TB
    else:
        total = sum(k.data_limit for k in keys if k.data_limit)

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
        "Profile-Title": "SecureAccess",
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
        "Profile-Title": "SecureAccess",
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


# ── 1-Click Node Installation & Auto-Registration ──

@router.get("/api/nodes/install/{install_token}", response_class=PlainTextResponse)
async def api_get_node_install_script(
    install_token: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Выдать автономный bash-скрипт установки ноды по токену."""
    from app.models import Server
    from app.node_installer import generate_install_script
    import os

    server = db.query(Server).filter(Server.install_token == install_token).first()
    if not server:
        raise HTTPException(status_code=404, detail="Скрипт установки не найден или токен недействителен")

    # Определяем базовый URL мастер-сервера для обратной регистрации
    env_domain = os.getenv("DOMAIN", "")
    if env_domain:
        proto = "http" if env_domain.startswith("http://") else ("https" if not env_domain.startswith("https://") else "")
        if proto:
            master_base_url = f"{proto}://{env_domain}"
        else:
            master_base_url = f"https://{env_domain}"
    else:
        host_header = request.headers.get("host", "127.0.0.1:8000")
        scheme = request.url.scheme or "https"
        master_base_url = f"{scheme}://{host_header}"

    script = generate_install_script(server, master_base_url)
    return PlainTextResponse(script, media_type="text/x-shellscript; charset=utf-8")


@router.post("/api/nodes/register")
async def api_register_node(
    request: Request,
    db: Session = Depends(get_db),
):
    """Обратный вызов от установщика ноды: регистрация ключей и мгновенный запуск."""
    import asyncio
    from app.models import Server
    from app.remote_xray import sync_server
    from app.cheburcheck import check_target
    from app.backup import SUPERADMIN_CHAT_ID, TELEGRAM_TOKEN
    import httpx

    body = await request.json()
    install_token = body.get("install_token")
    if not install_token:
        raise HTTPException(status_code=400, detail="install_token обязателен")

    server = db.query(Server).filter(Server.install_token == install_token).first()
    if not server:
        raise HTTPException(status_code=404, detail="Сервер с таким install_token не найден")

    # Обновляем данные сервера
    server.host = body.get("host") or server.host
    api_port = body.get("api_port", 9090)
    server.api_url = f"http://{server.host}:{api_port}"
    if body.get("reality_public_key"):
        server.reality_public_key = body["reality_public_key"]
    if body.get("reality_private_key"):
        server.reality_private_key = body["reality_private_key"]
    if body.get("reality_short_id"):
        server.reality_short_id = body["reality_short_id"]

    server.is_active = True
    server.last_sync_status = "syncing"
    db.commit()

    # 1. Формируем актуальный конфиг Sing-box со всеми ключами
    from app.remote_xray import _build_singbox_remote_config
    from app.models import VPNKey
    keys = db.query(VPNKey).filter(VPNKey.is_active == True).order_by(VPNKey.id).all()
    node_config = _build_singbox_remote_config(server, keys)

    server.last_sync = datetime.now(timezone.utc)
    server.last_sync_status = "ok"
    db.commit()

    # Попытка фонового push (если входящий порт доступен)
    asyncio.create_task(sync_server(db, server))

    # 2. Если роль каскад — перезапускаем мастер-Xray для создания маршрутов
    if server.role in ("cascade", "ru"):
        from app.xray import sync_and_reload
        try:
            sync_and_reload(db)
        except Exception as e:
            logger.warning("Не удалось перезагрузить мастер Xray для каскада %s: %s", server.name, e)

    # 3. Запускаем Cheburcheck проверку нового IP
    if server.host and server.host != "pending":
        asyncio.create_task(check_target(server.host))

    # 4. Telegram оповещение суперадмину
    if TELEGRAM_TOKEN and SUPERADMIN_CHAT_ID:
        async def _notify():
            try:
                async with httpx.AsyncClient(timeout=10.0, trust_env=False) as cl:
                    msg = (
                        f"🚀 <b>Новый узел успешно подключен!</b>\n\n"
                        f"• <b>Имя:</b> {server.name} ({server.region.upper()})\n"
                        f"• <b>Хост (IP):</b> <code>{server.host}</code>\n"
                        f"• <b>Роль:</b> {server.role}\n"
                        f"• <b>Протоколы:</b> REALITY ({server.reality_port or 443}), "
                        f"Hysteria 2 ({server.hysteria_port or 8443}), "
                        f"TUIC ({server.tuic_port or 8444})\n"
                        f"• <b>Статус:</b> Активен, ключи синхронизируются, добавлен в клиентские подписки."
                    )
                    await cl.post(
                        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                        json={"chat_id": SUPERADMIN_CHAT_ID, "text": msg, "parse_mode": "HTML"}
                    )
            except Exception as notify_err:
                logger.warning("Ошибка отправки Telegram оповещения о новом сервере: %s", notify_err)

        asyncio.create_task(_notify())

    return JSONResponse({
        "ok": True,
        "status": "registered",
        "id": server.id,
        "name": server.name,
        "host": server.host,
        "config": node_config,
    })


@router.get("/api/nodes/poll/{api_token}")
@router.post("/api/nodes/poll/{api_token}")
async def api_nodes_poll(
    api_token: str,
    db: Session = Depends(get_db),
):
    """Поллинг от ноды: получение актуального конфига и отправка heartbeat."""
    import hashlib
    import json
    from app.remote_xray import _build_singbox_remote_config
    from app.models import VPNKey

    server = db.query(Server).filter(Server.api_token == api_token).first()
    if not server:
        raise HTTPException(status_code=404, detail="Нода не найдена")

    keys = db.query(VPNKey).filter(VPNKey.is_active == True).order_by(VPNKey.id).all()
    node_config = _build_singbox_remote_config(server, keys)

    cfg_raw = json.dumps(node_config, sort_keys=True)
    cfg_hash = hashlib.sha256(cfg_raw.encode("utf-8")).hexdigest()[:16]

    server.last_sync = datetime.now(timezone.utc)
    server.last_sync_status = "ok"
    db.commit()

    return JSONResponse({
        "ok": True,
        "server_id": server.id,
        "name": server.name,
        "hash": cfg_hash,
        "config": node_config,
    })
