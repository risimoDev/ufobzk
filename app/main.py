"""FastAPI приложение — каскадный VPN на голом Xray."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.bruteforce import api_guard
from app.models import SUPERADMIN_TELEGRAM_ID, User, init_db
from app.xray import sync_and_reload

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
    from app.env_check import check_env_on_startup
    check_env_on_startup()
    # Ensure DB schema is current before querying
    try:
        from alembic.config import Config
        from alembic import command
        alembic_cfg = Config("alembic.ini")
        command.upgrade(alembic_cfg, "head")
    except Exception as e:
        logger.warning("Alembic upgrade skipped: %s", e)
    init_db()
    # If alembic_version is missing (tables created via create_all without tracking),
    # stamp to head via raw SQLite so future alembic calls don't re-create existing tables.
    try:
        import sqlite3 as _sqlite3
        _db_path = os.path.join("data", "vpnbzk.db")
        if os.path.exists(_db_path):
            with _sqlite3.connect(_db_path) as _sc:
                _tables = {r[0] for r in _sc.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                _av_rows = 0
                if "alembic_version" in _tables:
                    _av_rows = _sc.execute("SELECT COUNT(*) FROM alembic_version").fetchone()[0]
            # Stamp needed if: tables exist but alembic_version missing or empty
            if "users" in _tables and ("alembic_version" not in _tables or _av_rows == 0):
                # Get head revision ID from migration scripts (no DB connection needed)
                from alembic.config import Config as _ACfg
                from alembic.script import ScriptDirectory as _SD
                _head_rev = _SD.from_config(_ACfg("alembic.ini")).get_current_head()
                if _head_rev:
                    with _sqlite3.connect(_db_path) as _sc:
                        _sc.execute(
                            "CREATE TABLE IF NOT EXISTS alembic_version "
                            "(version_num VARCHAR(32) NOT NULL, "
                            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
                        )
                        _sc.execute("DELETE FROM alembic_version")
                        _sc.execute("INSERT INTO alembic_version (version_num) VALUES (?)", (_head_rev,))
                        _sc.commit()
                    logger.info("Alembic stamped to head (%s) via raw SQLite.", _head_rev)
    except Exception as e:
        logger.warning("Alembic stamp failed: %s", e)
    from app.models import SessionLocal
    _db = SessionLocal()
    try:
        try:
            if SUPERADMIN_TELEGRAM_ID:
                sa = _db.query(User).filter(User.telegram_id == SUPERADMIN_TELEGRAM_ID).first()
                if sa:
                    if not sa.is_admin:
                        sa.is_admin = True
                        _db.commit()
                else:
                    from app.models import _gen_uuid
                    sa = User(
                        telegram_id=SUPERADMIN_TELEGRAM_ID,
                        display_name="Суперадмин",
                        is_admin=True,
                        is_active=True,
                        sub_token=_gen_uuid(),
                    )
                    _db.add(sa)
                    _db.commit()
            else:
                logger.warning("SUPERADMIN_TELEGRAM_ID не задан — суперадмин не создан автоматически.")
        except Exception as e:
            _db.rollback()
            logger.warning("Superadmin check/create failed (schema mismatch?): %s", e)
        # Синхронизация Xray при старте
        try:
            sync_and_reload(_db)
        except Exception as e:
            logger.warning("Не удалось синхронизировать Xray при старте: %s", e)
    finally:
        _db.close()
    global bot_task
    bot_task = asyncio.create_task(_start_bot())
    # Фоновые задачи: сбор трафика + проверка истечения ключей
    from app.tasks import run_background_tasks
    bg_task = asyncio.create_task(run_background_tasks())
    logger.info("Приложение запущено (каскадный VPN).")
    yield
    if bot_task and not bot_task.done():
        bot_task.cancel()
    if not bg_task.done():
        bg_task.cancel()
    logger.info("Приложение остановлено.")


async def _start_bot():
    from app.bot import start_bot
    await start_bot()


app = FastAPI(title="VPNBZK Cascade", lifespan=lifespan)
app.state.limiter = limiter


# ── Security middleware (чистый ASGI) ──

BLOCKED_UA_KEYWORDS = ("sqlmap", "nikto", "nmap", "masscan", "zgrab", "dirbuster", "gobuster", "hydra")
BLOCKED_PATHS = (
    "/wp-admin", "/wp-login", "/xmlrpc.php", "/.env", "/phpmyadmin",
    "/admin.php", "/wp-content", "/wp-includes", "/.git", "/config.php",
    "/shell", "/cmd", "/eval", "/.aws", "/.ssh", "/actuator",
)

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
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

        request_headers = dict(scope.get("headers", []))
        ua = request_headers.get(b"user-agent", b"").decode("latin-1", errors="replace").lower()
        path = scope.get("path", "/").lower()

        xff = request_headers.get(b"x-forwarded-for", b"").decode("latin-1", errors="replace")
        if xff:
            client_ip = xff.split(",")[0].strip()
        else:
            client_ip = (scope.get("client") or ("0.0.0.0", 0))[0]

        if any(kw in ua for kw in BLOCKED_UA_KEYWORDS):
            logger.warning("Блокирован сканер: UA=%s IP=%s", ua[:80], client_ip)
            await self._send_json(send, 403, {"detail": "Forbidden"})
            return

        if any(path.startswith(bp) for bp in BLOCKED_PATHS):
            logger.warning("Блокирован зонд-путь: %s IP=%s", path, client_ip)
            api_guard.record_failure(client_ip)
            await self._send_json(send, 404, {"detail": "Not Found"})
            return

        if api_guard.is_blocked(client_ip):
            await self._send_json(send, 429, {"detail": "Доступ временно заблокирован."})
            return

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
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


# ── Подключение роутеров ──

from app.routers.public import router as public_router
from app.routers.cabinet import router as cabinet_router
from app.routers.admin import router as admin_router

app.include_router(public_router)
app.include_router(cabinet_router)
app.include_router(admin_router)
