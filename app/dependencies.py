"""Общие зависимости для роутеров: текущий пользователь, админ, CSRF, IP, логирование."""

import logging
import os

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import SESSION_COOKIE, generate_csrf_token, load_session_token, validate_csrf_token
from app.bruteforce import admin_guard
from app.models import User, get_db

logger = logging.getLogger(__name__)

# Общий экземпляр шаблонов для всех роутеров
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(_BASE_DIR, "templates"))
templates.env.globals["format_bytes"] = lambda b: _format_bytes(b)
templates.env.globals["csrf_token"] = generate_csrf_token


def _format_bytes(b: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} ПБ"


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


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = _get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
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


def verify_csrf(request: Request, token: str | None) -> None:
    if not validate_csrf_token(token):
        logger.warning("CSRF validation failed from %s", _client_ip(request))
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен. Перезагрузите страницу.")
