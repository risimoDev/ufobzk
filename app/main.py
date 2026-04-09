"""FastAPI приложение — маршруты, шаблоны, lifespan."""

import asyncio
import logging
import os
import time as _time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncGenerator

from fastapi import (
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import (
    SESSION_COOKIE,
    create_session_token,
    delete_invite_key,
    generate_csrf_token,
    generate_invite_key,
    list_invite_keys,
    load_session_token,
    validate_csrf_token,
    verify_code_and_get_user,
)
from app.bot import TELEGRAM_BOT_USERNAME, start_bot
from app.bruteforce import admin_guard, api_guard, login_guard
from app.marzban import MarzbanError, MarzbanNotFoundError, marzban
from app.models import SUPERADMIN_TELEGRAM_ID, AuditLog, User, get_db, init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_IPS = [ip.strip() for ip in os.getenv("ADMIN_IPS", "127.0.0.1").split(",") if ip.strip()]

# ── Rate limiter ──
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])

# ── Lifespan ──

bot_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Инициализация БД, обеспечение суперадмина и запуск бота при старте."""
    os.makedirs("data", exist_ok=True)
    init_db()
    from app.models import SessionLocal
    _db = SessionLocal()
    try:
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
    finally:
        _db.close()
    global bot_task
    bot_task = asyncio.create_task(start_bot())
    logger.info("Приложение запущено.")
    yield
    if bot_task and not bot_task.done():
        bot_task.cancel()
    logger.info("Приложение остановлено.")


app = FastAPI(title="VPNBZK", lifespan=lifespan)
app.state.limiter = limiter


# ── Security middleware ──


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Security headers + защита от основных атак на уровне приложения."""

    BLOCKED_UA_KEYWORDS = ("sqlmap", "nikto", "nmap", "masscan", "zgrab", "dirbuster", "gobuster", "hydra")
    BLOCKED_PATHS = (
        "/wp-admin", "/wp-login", "/xmlrpc.php", "/.env", "/phpmyadmin",
        "/admin.php", "/wp-content", "/wp-includes", "/.git", "/config.php",
        "/shell", "/cmd", "/eval", "/.aws", "/.ssh", "/actuator",
    )

    async def dispatch(self, request: Request, call_next):
        ip = _client_ip(request)

        # Блокировка сканеров по User-Agent
        ua = (request.headers.get("user-agent") or "").lower()
        if any(kw in ua for kw in self.BLOCKED_UA_KEYWORDS):
            logger.warning("Блокирован сканер: UA=%s IP=%s", ua[:80], ip)
            return JSONResponse({"detail": "Forbidden"}, status_code=403)

        # Блокировка типичных путей сканирования
        path = request.url.path.lower()
        if any(path.startswith(bp) for bp in self.BLOCKED_PATHS):
            logger.warning("Блокирован зонд-путь: %s IP=%s", path, ip)
            api_guard.record_failure(ip)
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        # Блокировка по API brute-force guard
        if api_guard.is_blocked(ip):
            return JSONResponse({"detail": "Доступ временно заблокирован."}, status_code=429)

        response = await call_next(request)

        # Дополнительные security headers (дублируют nginx для случая прямого доступа)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-XSS-Protection", "1; mode=block")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate")
        response.headers.setdefault("Pragma", "no-cache")

        return response


app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse({"detail": "Слишком много запросов. Попробуйте позже."}, status_code=429)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "static")),
    name="static",
)
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ── Хелперы ──

def _client_ip(request: Request) -> str:
    """Реальный IP клиента (с учётом X-Forwarded-For за nginx)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _get_current_user(request: Request, db: Session) -> User | None:
    """Получить текущего пользователя из подписанной cookie-сессии."""
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
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def _require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    ip = _client_ip(request)

    # Проверка brute-force бана на админку
    if admin_guard.is_blocked(ip):
        raise HTTPException(status_code=429, detail="Доступ временно заблокирован")

    user = _get_current_user(request, db)
    if not user or not user.is_admin:
        admin_guard.record_failure(ip)
        logger.warning("Неудачный доступ к админке: IP=%s", ip)
        raise HTTPException(status_code=403, detail="Доступ запрещён")
    # IP-фильтрация на уровне nginx (whitelist.conf)
    return user


def _verify_csrf(request: Request, token: str | None) -> None:
    """Проверить CSRF-токен в POST-форме. Бросает 403 при ошибке."""
    if not validate_csrf_token(token):
        logger.warning("CSRF validation failed from %s", _client_ip(request))
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен. Перезагрузите страницу.")


def _log_action(db: Session, admin_id: int, action: str, target: str = "", detail: str = "") -> None:
    """Записать действие администратора в audit log."""
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
    """Проверка здоровья: БД + Marzban."""
    result: dict[str, Any] = {"status": "ok", "db": "ok", "marzban": "unknown"}
    try:
        db.execute(sa_text("SELECT 1"))
    except Exception:
        result["db"] = "error"
        result["status"] = "degraded"
    try:
        await marzban.get_node_stats()
        result["marzban"] = "ok"
    except MarzbanError:
        result["marzban"] = "unavailable"
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

    # Проверка brute-force бана
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
    user = verify_code_and_get_user(db, code.strip())

    if user is None:
        login_guard.record_failure(ip)
        logger.warning("Неудачный вход: IP=%s code=%s", ip, code[:2] + "****")
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Неверный или просроченный код. Убедитесь, что вы зарегистрированы через бота.",
                "bot_username": TELEGRAM_BOT_USERNAME,
                "csrf_token": generate_csrf_token(),
            },
        )

    login_guard.record_success(ip)
    response = RedirectResponse(url="/cabinet", status_code=303)
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


# ── Личный кабинет ──


@app.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request, db: Session = Depends(get_db)):
    user = _get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    vpn_data = None
    config_links: list[str] = []
    subscription_url: str = ""
    error = None

    expire_info = {"days_left": None, "date_str": None}

    if user.marzban_username:
        try:
            vpn_data = await marzban.get_user(user.marzban_username)
            sub_info = await marzban.get_subscription_links(user.marzban_username)
            config_links = sub_info.get("links", [])
            subscription_url = sub_info.get("subscription_url", "")
            if vpn_data and vpn_data.get("expire"):
                ts = vpn_data["expire"]
                expire_info["days_left"] = max(0, int((ts - _time.time()) / 86400))
                expire_info["date_str"] = datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
        except Exception as e:
            logger.error("Ошибка Marzban API: %s", e)
            error = "Не удалось получить данные VPN."

    return templates.TemplateResponse(
        "cabinet.html",
        {
            "request": request,
            "user": user,
            "vpn": vpn_data,
            "config_links": config_links,
            "subscription_url": subscription_url,
            "expire_info": expire_info,
            "error": error,
        },
    )


# ── Админ-панель ──


def _marzban_vpn_status_counts(marzban_users: list[dict]) -> dict[str, int]:
    """Подсчитать статусы VPN-пользователей Marzban."""
    counts: dict[str, int] = {"active": 0, "expired": 0, "limited": 0, "disabled": 0}
    for mu in marzban_users:
        s = mu.get("status", "")
        if s in counts:
            counts[s] += 1
    return counts


def _total_traffic(marzban_users: list[dict]) -> int:
    """Суммарный трафик всех VPN-пользователей (байт)."""
    return sum(mu.get("used_traffic", 0) for mu in marzban_users)


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    admin = _require_admin(request, db)

    # Локальные пользователи
    all_users = db.query(User).order_by(User.id).all()
    total_local = len(all_users)
    active_local = sum(1 for u in all_users if u.is_active)
    with_vpn = sum(1 for u in all_users if u.marzban_username)

    # Marzban-пользователи и статистика (graceful degradation)
    marzban_users: list[dict] = []
    system_stats: dict[str, Any] = {}
    marzban_error: str | None = None
    try:
        marzban_users = await marzban.get_all_users()
    except MarzbanError as e:
        logger.error("Ошибка получения пользователей Marzban: %s", e)
        marzban_error = f"Marzban недоступен: {e}"
    try:
        system_stats = await marzban.get_node_stats()
    except MarzbanError as e:
        logger.error("Ошибка получения системной статистики: %s", e)

    vpn_counts = _marzban_vpn_status_counts(marzban_users)
    traffic_total = _total_traffic(marzban_users)

    # Совмещённая таблица: локальный User + Marzban данные
    marzban_map = {mu.get("username"): mu for mu in marzban_users}
    enriched_users = []
    for u in all_users:
        vpn = marzban_map.get(u.marzban_username) if u.marzban_username else None
        expire_str = None
        days_left = None
        if vpn and vpn.get("expire"):
            ts = vpn["expire"]
            days_left = max(0, int((ts - _time.time()) / 86400))
            expire_str = datetime.fromtimestamp(ts).strftime("%d.%m.%Y")
        enriched_users.append({
            "user": u,
            "vpn": vpn,
            "expire_str": expire_str,
            "days_left": days_left,
        })

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "user": admin,
            "enriched_users": enriched_users,
            "total_local": total_local,
            "active_local": active_local,
            "with_vpn": with_vpn,
            "vpn_counts": vpn_counts,
            "traffic_total": traffic_total,
            "system_stats": system_stats,
            "marzban_users": marzban_users,
            "marzban_error": marzban_error,
            "invite_keys": list_invite_keys(db),
            "csrf_token": generate_csrf_token(),
        },
    )


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(request: Request, db: Session = Depends(get_db)):
    """Отдельная страница списка пользователей (перенаправляет на дашборд)."""
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/users")
@limiter.limit("10/minute")
async def admin_create_user(
    request: Request,
    display_name: str = Form(""),
    telegram_id: int = Form(...),
    data_limit_gb: float = Form(0),
    expire_days: int = Form(0),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    """Создание нового пользователя: запись в БД + VPN-аккаунт в Marzban."""
    admin = _require_admin(request, db)
    _verify_csrf(request, csrf_token)

    # Валидация ввода
    if telegram_id < 1 or telegram_id > 9_999_999_999:
        raise HTTPException(status_code=400, detail="Некорректный Telegram ID")
    if data_limit_gb < 0 or data_limit_gb > 10_000:
        raise HTTPException(status_code=400, detail="Некорректный лимит трафика")
    if expire_days < 0 or expire_days > 3650:
        raise HTTPException(status_code=400, detail="Некорректный срок")
    display_name = display_name.strip()[:100]

    # Проверяем, нет ли такого telegram_id
    exists = db.query(User).filter(User.telegram_id == telegram_id).first()
    if exists:
        raise HTTPException(status_code=409, detail="Пользователь с таким Telegram ID уже существует")

    # Генерируем VPN-username
    vpn_username = f"vpn_{telegram_id}"

    # Шаг 1: Создаём в Marzban
    try:
        await marzban.create_user(
            username=vpn_username,
            data_limit_gb=data_limit_gb,
            expire_days=expire_days,
        )
    except MarzbanError as e:
        logger.error("Ошибка создания VPN: %s", e)
        raise HTTPException(status_code=500, detail=f"Ошибка Marzban: {e}")

    # Шаг 2: Создаём локального пользователя
    new_user = User(
        telegram_id=telegram_id,
        display_name=display_name or None,
        marzban_username=vpn_username,
        is_active=True,
    )
    db.add(new_user)
    db.commit()

    _log_action(db, admin.id, "create_user", str(telegram_id), f"vpn={vpn_username}")

    return RedirectResponse(url="/admin", status_code=303)


@app.put("/admin/users/{user_id}")
async def admin_edit_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Редактирование пользователя: продление, лимит, сброс трафика."""
    admin = _require_admin(request, db)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    body = await request.json()

    # Обновить display_name
    if "display_name" in body:
        target.display_name = body["display_name"].strip() or None
        db.commit()

    # Обновить is_active
    if "is_active" in body:
        target.is_active = bool(body["is_active"])
        db.commit()

    # Обновить is_admin
    if "is_admin" in body:
        target.is_admin = bool(body["is_admin"])
        db.commit()

    # Marzban-обновления
    if target.marzban_username:
        marzban_kwargs: dict[str, Any] = {}

        if "data_limit_gb" in body:
            marzban_kwargs["data_limit_gb"] = float(body["data_limit_gb"])
        if "expire_days" in body:
            marzban_kwargs["expire_days"] = int(body["expire_days"])
        if "reset_traffic" in body and body["reset_traffic"]:
            try:
                await marzban.reset_user_traffic(target.marzban_username)
            except MarzbanError as e:
                logger.error("Ошибка сброса трафика: %s", e)

        if marzban_kwargs:
            try:
                await marzban.update_user(target.marzban_username, **marzban_kwargs)
            except MarzbanError as e:
                logger.error("Ошибка обновления VPN: %s", e)
                return JSONResponse({"error": str(e)}, status_code=500)

    _log_action(db, admin.id, "edit_user", str(target.telegram_id), str(body))
    return JSONResponse({"ok": True})


@app.delete("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Удаление пользователя из БД и Marzban."""
    admin = _require_admin(request, db)

    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    target_info = f"tg={target.telegram_id} vpn={target.marzban_username}"

    # Удалить из Marzban
    if target.marzban_username:
        try:
            await marzban.delete_user(target.marzban_username)
        except MarzbanNotFoundError:
            logger.warning("Пользователь %s уже удалён из Marzban", target.marzban_username)
        except MarzbanError as e:
            logger.error("Ошибка удаления из Marzban: %s", e)

    db.delete(target)
    db.commit()
    _log_action(db, admin.id, "delete_user", target_info)
    return JSONResponse({"ok": True})


# ── Обратная совместимость старых POST-маршрутов ──


# ── Инвайт-ключи ──


@app.post("/admin/keys")
async def admin_create_key(
    request: Request,
    db: Session = Depends(get_db),
):
    """Создание нового инвайт-ключа."""
    admin = _require_admin(request, db)

    invite = generate_invite_key(db, admin.id)
    _log_action(db, admin.id, "create_key", invite.key)
    return JSONResponse({"ok": True, "key": invite.key, "id": invite.id})


@app.get("/admin/keys")
async def admin_list_keys(
    request: Request,
    db: Session = Depends(get_db),
):
    """Список всех инвайт-ключей (JSON)."""
    _require_admin(request, db)

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


@app.delete("/admin/keys/{key_id}")
async def admin_delete_key(
    key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Удаление инвайт-ключа."""
    admin = _require_admin(request, db)

    if not delete_invite_key(db, key_id):
        raise HTTPException(status_code=404, detail="Ключ не найден")

    _log_action(db, admin.id, "delete_key", str(key_id))
    return JSONResponse({"ok": True})


# ── Обратная совместимость старых POST-маршрутов (legacy) ──


@app.post("/admin/link")
async def admin_link_user(
    request: Request,
    user_id: int = Form(...),
    marzban_username: str = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    _verify_csrf(request, csrf_token)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    target.marzban_username = marzban_username.strip()[:64]
    db.commit()
    _log_action(db, admin.id, "link_marzban", str(target.telegram_id), marzban_username.strip())
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/toggle")
async def admin_toggle_user(
    request: Request,
    user_id: int = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    _verify_csrf(request, csrf_token)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    target.is_active = not target.is_active
    db.commit()
    _log_action(db, admin.id, "toggle_active", str(target.telegram_id))
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/make-admin")
async def admin_make_admin(
    request: Request,
    user_id: int = Form(...),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    _verify_csrf(request, csrf_token)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    target.is_admin = not target.is_admin
    db.commit()
    _log_action(db, admin.id, "toggle_admin", str(target.telegram_id))
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/create-vpn")
async def admin_create_vpn_user(
    request: Request,
    user_id: int = Form(...),
    vpn_username: str = Form(...),
    data_limit_gb: float = Form(0),
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    admin = _require_admin(request, db)
    _verify_csrf(request, csrf_token)
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    try:
        await marzban.create_user(
            username=vpn_username.strip()[:64],
            data_limit_gb=data_limit_gb,
        )
        target.marzban_username = vpn_username.strip()[:64]
        db.commit()
    except MarzbanError as e:
        logger.error("Ошибка создания VPN-пользователя: %s", e)
        raise HTTPException(status_code=500, detail=f"Ошибка Marzban: {e}")
    return RedirectResponse(url="/admin", status_code=303)


# ── Brute-force управление ──


@app.get("/admin/security")
async def admin_security_stats(request: Request, db: Session = Depends(get_db)):
    """Статистика безопасности: брутфорс-гарды, аудит-лог."""
    _require_admin(request, db)
    return JSONResponse({
        "login_guard": login_guard.get_stats(),
        "admin_guard": admin_guard.get_stats(),
        "api_guard": api_guard.get_stats(),
    })


@app.post("/admin/unban")
async def admin_unban_ip(
    request: Request,
    db: Session = Depends(get_db),
):
    """Ручная разблокировка IP."""
    admin = _require_admin(request, db)
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
