"""FastAPI приложение — каскадный VPN на голом Xray."""

import asyncio
import logging
import os
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, AsyncGenerator

from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.auth import (
    SESSION_COOKIE,
    create_session_token,
    delete_invite_key,
    generate_csrf_token,
    generate_invite_key,
    list_invite_keys,
    load_session_token,
    validate_csrf_token,
    verify_admin_password,
    verify_code_and_get_user,
)
from app.bot import TELEGRAM_BOT_USERNAME, start_bot
from app.bruteforce import admin_guard, api_guard, login_guard
from app.models import SUPERADMIN_TELEGRAM_ID, AuditLog, User, VPNKey, get_db, init_db
from app.xray import (
    generate_uuid,
    get_subscription_content,
    get_user_links,
    sync_and_reload,
    GB,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Rate limiter ──
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])

# ── Lifespan ──

bot_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Инициализация БД, суперадмин, синхронизация Xray и запуск бота."""
    os.makedirs("data", exist_ok=True)
    init_db()
    from app.models import SessionLocal
    _db = SessionLocal()
    try:
        if SUPERADMIN_TELEGRAM_ID:
            sa = _db.query(User).filter(User.telegram_id == SUPERADMIN_TELEGRAM_ID).first()
            if sa:
                if not sa.is_admin:
                    sa.is_admin = True
                    _db.commit()
            else:
                sa = User(
                    telegram_id=SUPERADMIN_TELEGRAM_ID,
                    display_name="Суперадмин",
                    is_admin=True,
                    is_active=True,
                )
                _db.add(sa)
                _db.commit()
        else:
            logger.warning("SUPERADMIN_TELEGRAM_ID не задан — суперадмин не создан автоматически.")
        # Синхронизация Xray при старте
        try:
            sync_and_reload(_db)
        except Exception as e:
            logger.warning("Не удалось синхронизировать Xray при старте: %s", e)
    finally:
        _db.close()
    global bot_task
    bot_task = asyncio.create_task(start_bot())
    logger.info("Приложение запущено (каскадный VPN).")
    yield
    if bot_task and not bot_task.done():
        bot_task.cancel()
    logger.info("Приложение остановлено.")


app = FastAPI(title="VPNBZK Cascade", lifespan=lifespan)
app.state.limiter = limiter


# ── Security middleware (чистый ASGI — НЕ BaseHTTPMiddleware, который ломает exception handling) ──

BLOCKED_UA_KEYWORDS = ("sqlmap", "nikto", "nmap", "masscan", "zgrab", "dirbuster", "gobuster", "hydra")
BLOCKED_PATHS = (
    "/wp-admin", "/wp-login", "/xmlrpc.php", "/.env", "/phpmyadmin",
    "/admin.php", "/wp-content", "/wp-includes", "/.git", "/config.php",
    "/shell", "/cmd", "/eval", "/.aws", "/.ssh", "/actuator",
)

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"x-xss-protection", b"1; mode=block"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"content-security-policy", (
        b"default-src 'self'; "
        b"script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
        b"style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://fonts.googleapis.com; "
        b"font-src 'self' https://fonts.gstatic.com; "
        b"img-src 'self' data:; "
        b"connect-src 'self'; "
        b"frame-ancestors 'none'"
    )),
    (b"cache-control", b"no-store, no-cache, must-revalidate"),
    (b"pragma", b"no-cache"),
]


class SecurityMiddleware:
    """Чистый ASGI middleware — не ломает обработку исключений FastAPI."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Извлекаем заголовки запроса
        request_headers = dict(scope.get("headers", []))
        ua = request_headers.get(b"user-agent", b"").decode("latin-1", errors="replace").lower()
        path = scope.get("path", "/").lower()

        # Определяем IP клиента
        xff = request_headers.get(b"x-forwarded-for", b"").decode("latin-1", errors="replace")
        if xff:
            client_ip = xff.split(",")[0].strip()
        else:
            client_ip = (scope.get("client") or ("0.0.0.0", 0))[0]

        # Блок сканеров по User-Agent
        if any(kw in ua for kw in BLOCKED_UA_KEYWORDS):
            logger.warning("Блокирован сканер: UA=%s IP=%s", ua[:80], client_ip)
            await self._send_json(send, 403, {"detail": "Forbidden"})
            return

        # Блок зонд-путей
        if any(path.startswith(bp) for bp in BLOCKED_PATHS):
            logger.warning("Блокирован зонд-путь: %s IP=%s", path, client_ip)
            api_guard.record_failure(client_ip)
            await self._send_json(send, 404, {"detail": "Not Found"})
            return

        # IP заблокирован brute-force guard
        if api_guard.is_blocked(client_ip):
            await self._send_json(send, 429, {"detail": "Доступ временно заблокирован."})
            return

        # Оборачиваем send для добавления security-заголовков
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                existing_headers = list(message.get("headers", []))
                existing_names = {h[0].lower() for h in existing_headers}
                for name, value in SECURITY_HEADERS:
                    if name not in existing_names:
                        existing_headers.append((name, value))
                message = {**message, "headers": existing_headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _send_json(send, status_code: int, body: dict):
        import json as _json
        payload = _json.dumps(body).encode()
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": payload,
        })


app.add_middleware(SecurityMiddleware)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse({"detail": "Слишком много запросов. Попробуйте позже."}, status_code=429)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """Явный обработчик HTTPException — гарантирует 302-редиректы."""
    if exc.status_code in (301, 302, 303, 307, 308):
        location = (exc.headers or {}).get("Location", "/")
        return RedirectResponse(url=location, status_code=exc.status_code)
    if exc.status_code == 403:
        return JSONResponse({"detail": exc.detail or "Forbidden"}, status_code=403)
    if exc.status_code == 429:
        return JSONResponse({"detail": exc.detail or "Too Many Requests"}, status_code=429)
    return JSONResponse({"detail": exc.detail or "Error"}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    logger.error("Необработанная ошибка на %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse({"detail": "Internal Server Error", "error": str(exc)}, status_code=500)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ── Хелперы ──


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _get_current_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id = load_session_token(token)
    if user_id is None:
        return None
    return db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712


def _require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = _get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def _require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    ip = _client_ip(request)
    if admin_guard.is_blocked(ip):
        raise HTTPException(status_code=429, detail="Доступ временно заблокирован")
    user = _get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    if not user.is_admin:
        admin_guard.record_failure(ip)
        logger.warning("Неудачный доступ к админке: IP=%s user=%s", ip, user.telegram_id)
        raise HTTPException(status_code=403, detail="Доступ запрещён")
    return user


def _verify_csrf(request: Request, token: str | None) -> None:
    if not validate_csrf_token(token):
        logger.warning("CSRF validation failed from %s", _client_ip(request))
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен. Перезагрузите страницу.")


def _log_action(db: Session, admin_id: int, action: str, target: str = "", detail: str = "") -> None:
    db.add(AuditLog(admin_id=admin_id, action=action, target=target, detail=detail))
    db.commit()


def _format_bytes(b: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} ПБ"


templates.env.globals["format_bytes"] = _format_bytes
templates.env.globals["csrf_token"] = generate_csrf_token


# ── Health endpoint ──


@app.get("/health")
async def health(db: Session = Depends(get_db)):
    result: dict[str, Any] = {"status": "ok", "db": "ok"}
    try:
        db.execute(sa_text("SELECT 1"))
    except Exception:
        result["db"] = "error"
        result["status"] = "degraded"
    code = 200 if result["status"] == "ok" else 503
    return JSONResponse(result, status_code=code)


# ── Публичные страницы ──


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/login", response_class=HTMLResponse)
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


@app.post("/login")
@limiter.limit("5/minute")
async def login_submit(
    request: Request,
    code: str = Form(...),
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

    _verify_csrf(request, csrf_token)

    user = None

    # ── Сначала пробуем пароль администратора ──
    if verify_admin_password(code.strip()):
        user = db.query(User).filter(
            User.telegram_id == SUPERADMIN_TELEGRAM_ID,
            User.is_active == True,  # noqa: E712
        ).first() if SUPERADMIN_TELEGRAM_ID else None
        if user is None:
            # fallback: любой активный admin
            user = db.query(User).filter(
                User.is_admin == True,  # noqa: E712
                User.is_active == True,  # noqa: E712
            ).first()
        if user is None:
            logger.warning("Верный ADMIN_PASSWORD, но admin-пользователь не найден в БД (IP=%s)", ip)

    # ── Затем пробуем одноразовый Telegram-код ──
    if user is None:
        user = verify_code_and_get_user(db, code.strip())

    if user is None:
        login_guard.record_failure(ip)
        logger.warning("Неудачный вход: IP=%s", ip)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Неверный код или пароль.",
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


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


# ── Подписка (для VPN-клиентов) ──


@app.get("/sub/{user_id}")
async def subscription(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Эндпоинт подписки — возвращает base64 список ссылок."""
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712
    if not user:
        raise HTTPException(status_code=404)
    keys = [k for k in user.vpn_keys if k.status == "active"]
    if not keys:
        raise HTTPException(status_code=404)
    content = get_subscription_content(keys)
    return PlainTextResponse(content, headers={
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": "inline",
        "Profile-Update-Interval": "12",
        "Subscription-Userinfo": f"upload=0; download={sum(k.data_used for k in keys)}; total={sum(k.data_limit or 0 for k in keys)}",
    })


# ── Личный кабинет ──


@app.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request, db: Session = Depends(get_db)):
    user = _get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    # Загружаем ключи пользователя с ссылками
    keys_data = []
    for key in user.vpn_keys:
        links = get_user_links(key)
        keys_data.append({
            "key": key,
            "links": links,
            "status": key.status,
        })

    webapp_url = os.getenv("WEBAPP_URL", "https://vpn.example.com")
    subscription_url = f"{webapp_url}/sub/{user.id}" if user.vpn_keys else ""

    return templates.TemplateResponse(
        "cabinet.html",
        {
            "request": request,
            "user": user,
            "keys_data": keys_data,
            "subscription_url": subscription_url,
        },
    )


# ── Админ-панель ──


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, admin: User = Depends(_require_admin), db: Session = Depends(get_db)):

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
        })

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
        },
    )


# ── Создание пользователя ──


@app.post("/admin/users")
@limiter.limit("10/minute")
async def admin_create_user(
    request: Request,
    display_name: str = Form(""),
    telegram_id: int = Form(...),
    data_limit_gb: float = Form(0),
    expire_days: int = Form(0),
    key_name: str = Form("default"),
    csrf_token: str = Form(""),
    admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    _verify_csrf(request, csrf_token)

    if telegram_id < 1 or telegram_id > 9_999_999_999:
        raise HTTPException(status_code=400, detail="Некорректный Telegram ID")
    if data_limit_gb < 0 or data_limit_gb > 10_000:
        raise HTTPException(status_code=400, detail="Некорректный лимит трафика")
    if expire_days < 0 or expire_days > 3650:
        raise HTTPException(status_code=400, detail="Некорректный срок")
    display_name = display_name.strip()[:100]
    key_name = key_name.strip()[:64] or "default"

    exists = db.query(User).filter(User.telegram_id == telegram_id).first()
    if exists:
        raise HTTPException(status_code=409, detail="Пользователь с таким Telegram ID уже существует")

    new_user = User(
        telegram_id=telegram_id,
        display_name=display_name or None,
        is_active=True,
    )
    db.add(new_user)
    db.flush()

    # Создаём первый VPN-ключ
    vpn_key = VPNKey(
        user_id=new_user.id,
        name=key_name,
        uuid=generate_uuid(),
        protocol="vless",
        data_limit=int(data_limit_gb * GB) if data_limit_gb > 0 else None,
        expire_at=datetime.utcnow() + timedelta(days=expire_days) if expire_days > 0 else None,
    )
    db.add(vpn_key)
    db.commit()

    # Синхронизация Xray
    try:
        sync_and_reload(db)
    except Exception as e:
        logger.error("Ошибка синхронизации Xray: %s", e)

    _log_action(db, admin.id, "create_user", str(telegram_id), f"key={vpn_key.uuid}")
    return RedirectResponse(url="/admin", status_code=303)


# ── Добавить ключ пользователю ──


@app.post("/admin/users/{user_id}/keys")
async def admin_add_key(
    user_id: int,
    request: Request,
    admin: User = Depends(_require_admin),
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
        expire_at=datetime.utcnow() + timedelta(days=expire_days) if expire_days > 0 else None,
    )
    db.add(vpn_key)
    db.commit()

    try:
        sync_and_reload(db)
    except Exception as e:
        logger.error("Ошибка синхронизации Xray: %s", e)

    _log_action(db, admin.id, "add_key", str(target.telegram_id), f"key={vpn_key.uuid} name={key_name}")
    return JSONResponse({"ok": True, "key_id": vpn_key.id, "uuid": vpn_key.uuid})


# ── Редактирование пользователя ──


@app.put("/admin/users/{user_id}")
async def admin_edit_user(
    user_id: int,
    request: Request,
    admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    body = await request.json()

    if "display_name" in body:
        target.display_name = str(body["display_name"]).strip()[:100] or None
    if "is_active" in body:
        target.is_active = bool(body["is_active"])
    if "is_admin" in body:
        target.is_admin = bool(body["is_admin"])
    db.commit()

    _log_action(db, admin.id, "edit_user", str(target.telegram_id), str(body))
    return JSONResponse({"ok": True})


# ── Редактирование ключа ──


@app.put("/admin/keys/{key_id}")
async def admin_edit_key(
    key_id: int,
    request: Request,
    admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    key = db.query(VPNKey).filter(VPNKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Ключ не найден")

    body = await request.json()
    need_reload = False

    if "name" in body:
        key.name = str(body["name"]).strip()[:64]
    if "is_active" in body:
        key.is_active = bool(body["is_active"])
        need_reload = True
    if "data_limit_gb" in body:
        gb = float(body["data_limit_gb"])
        key.data_limit = int(gb * GB) if gb > 0 else None
    if "expire_days" in body:
        days = int(body["expire_days"])
        key.expire_at = datetime.utcnow() + timedelta(days=days) if days > 0 else None
    if "reset_traffic" in body and body["reset_traffic"]:
        key.data_used = 0

    db.commit()

    if need_reload:
        try:
            sync_and_reload(db)
        except Exception as e:
            logger.error("Ошибка синхронизации Xray: %s", e)

    _log_action(db, admin.id, "edit_key", str(key.uuid), str(body))
    return JSONResponse({"ok": True})


# ── Удаление пользователя ──


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int,
    request: Request,
    admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    target_info = f"tg={target.telegram_id}"
    db.delete(target)  # cascade удалит и ключи
    db.commit()

    try:
        sync_and_reload(db)
    except Exception as e:
        logger.error("Ошибка синхронизации Xray: %s", e)

    _log_action(db, admin.id, "delete_user", target_info)
    return JSONResponse({"ok": True})


# ── Удаление ключа ──


@app.delete("/admin/vpnkeys/{key_id}")
async def admin_delete_key(
    key_id: int,
    request: Request,
    admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    key = db.query(VPNKey).filter(VPNKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Ключ не найден")

    key_info = f"uuid={key.uuid} user_id={key.user_id}"
    db.delete(key)
    db.commit()

    try:
        sync_and_reload(db)
    except Exception as e:
        logger.error("Ошибка синхронизации Xray: %s", e)

    _log_action(db, admin.id, "delete_vpnkey", key_info)
    return JSONResponse({"ok": True})


# ── Инвайт-ключи ──


@app.post("/admin/invite-keys")
async def admin_create_invite_key(
    request: Request,
    admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    invite = generate_invite_key(db, admin.id)
    _log_action(db, admin.id, "create_key", invite.key)
    return JSONResponse({"ok": True, "key": invite.key, "id": invite.id})


@app.get("/admin/invite-keys")
async def admin_list_invite_keys(
    request: Request,
    _: User = Depends(_require_admin),
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


@app.delete("/admin/invite-keys/{key_id}")
async def admin_delete_invite_key(
    key_id: int,
    request: Request,
    admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    if not delete_invite_key(db, key_id):
        raise HTTPException(status_code=404, detail="Ключ не найден")
    _log_action(db, admin.id, "delete_invite_key", str(key_id))
    return JSONResponse({"ok": True})


# ── Синхронизация Xray вручную ──


@app.post("/admin/sync-xray")
async def admin_sync_xray(
    request: Request,
    admin: User = Depends(_require_admin),
    db: Session = Depends(get_db),
):
    try:
        success = sync_and_reload(db)
        _log_action(db, admin.id, "sync_xray", "", f"success={success}")
        return JSONResponse({"ok": success})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── Brute-force управление ──


@app.get("/admin/security")
async def admin_security_stats(request: Request, _: User = Depends(_require_admin), db: Session = Depends(get_db)):
    return JSONResponse({
        "login_guard": login_guard.get_stats(),
        "admin_guard": admin_guard.get_stats(),
        "api_guard": api_guard.get_stats(),
    })


@app.post("/admin/unban")
async def admin_unban_ip(
    request: Request,
    admin: User = Depends(_require_admin),
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
