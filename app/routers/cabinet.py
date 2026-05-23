"""Роуты личного кабинета пользователя."""

import logging
import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app.auth import hash_password, verify_user_credentials
from app.dependencies import _get_current_user, templates, verify_csrf
from app.models import DEFAULT_SETTINGS, User, get_db, get_setting
from app.xray import DOMAIN, get_all_links

logger = logging.getLogger(__name__)
router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


@router.get("/cabinet", response_class=HTMLResponse)
async def cabinet(request: Request, db: Session = Depends(get_db)):
    user = _get_current_user(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    webapp_url = os.getenv("WEBAPP_URL", "https://vpn.example.com")
    keys_data = []
    for key in user.vpn_keys:
        all_links = get_all_links(db, key)
        ws_links = [l for l in all_links if l["type"] == "vless-ws"]
        keys_data.append({
            "key": key,
            "links": ws_links,
            "all_links": all_links,
            "status": key.status,
            "key_sub_url": f"{webapp_url}/sub/key/{key.uuid}",
        })

    sub_token = user.sub_token or ""
    subscription_url = f"{webapp_url}/sub/{sub_token}" if user.vpn_keys and sub_token else ""

    settings = {k: get_setting(db, k) or DEFAULT_SETTINGS.get(k, "") for k in DEFAULT_SETTINGS}

    return templates.TemplateResponse(
        "cabinet.html",
        {
            "request": request,
            "user": user,
            "keys_data": keys_data,
            "subscription_url": subscription_url,
            "has_password": bool(user.password_hash),
            "settings": settings,
        },
    )


@router.post("/cabinet/change-password")
@limiter.limit("5/minute")
async def cabinet_change_password(
    request: Request,
    db: Session = Depends(get_db),
):
    user = _get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    body = await request.json()
    old_pw = str(body.get("old_password", "")).strip()
    new_pw = str(body.get("new_password", "")).strip()

    if not user.password_hash:
        raise HTTPException(status_code=400, detail="У вашего аккаунта нет пароля (вход через Telegram). Обратитесь к администратору.")
    if not old_pw:
        raise HTTPException(status_code=400, detail="Введите текущий пароль")
    if not verify_user_credentials(db, user.username or "", old_pw):
        raise HTTPException(status_code=400, detail="Неверный текущий пароль")
    if len(new_pw) < 4:
        raise HTTPException(status_code=400, detail="Новый пароль минимум 4 символа")
    if old_pw == new_pw:
        raise HTTPException(status_code=400, detail="Новый пароль совпадает со старым")

    user.password_hash = hash_password(new_pw)
    db.commit()
    return JSONResponse({"ok": True})


@router.post("/cabinet/rotate-sub-token")
@limiter.limit("5/minute")
async def cabinet_rotate_sub_token(
    request: Request,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    """Генерирует новый sub_token — старая ссылка подписки перестаёт работать."""
    user = _get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    verify_csrf(request, csrf_token)
    user.sub_token = secrets.token_urlsafe(32)
    user.sub_token_updated_at = datetime.now(timezone.utc)
    db.commit()
    return RedirectResponse(url="/cabinet", status_code=303)
