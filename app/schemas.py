"""Общая валидация и создание пользователей/ключей — устраняет дублирование между form и JSON API."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.models import User, VPNKey, _gen_uuid
from app.xray import GB, generate_uuid


@dataclass
class CreateUserData:
    display_name: str | None
    telegram_id: int | None
    username: str | None
    password: str | None
    data_limit_gb: float
    expire_days: int
    key_name: str


def validate_create_user(
    display_name: str = "",
    telegram_id: int = 0,
    username: str = "",
    password: str = "",
    data_limit_gb: float = 0,
    expire_days: int = 0,
    key_name: str = "default",
) -> CreateUserData:
    """Валидирует данные для создания пользователя. Бросает HTTPException при ошибках."""
    username = username.strip()[:64]
    password = password.strip()

    if telegram_id and (telegram_id < 0 or telegram_id > 9_999_999_999):
        raise HTTPException(status_code=400, detail="Некорректный Telegram ID")
    if not display_name and not telegram_id and not username:
        raise HTTPException(status_code=400, detail="Укажите хотя бы имя пользователя")
    if username and len(username) < 3:
        raise HTTPException(status_code=400, detail="Логин должен быть не менее 3 символов")
    if username and not password:
        raise HTTPException(status_code=400, detail="Укажите пароль для учётной записи с логином")
    if password and len(password) < 4:
        raise HTTPException(status_code=400, detail="Пароль должен быть не менее 4 символов")
    if data_limit_gb < 0 or data_limit_gb > 10_000:
        raise HTTPException(status_code=400, detail="Некорректный лимит трафика")
    if expire_days < 0 or expire_days > 3650:
        raise HTTPException(status_code=400, detail="Некорректный срок")

    display_name = display_name.strip()[:100]
    key_name = key_name.strip()[:64] or "default"

    return CreateUserData(
        display_name=display_name or None,
        telegram_id=telegram_id if telegram_id else None,
        username=username or None,
        password=password or None,
        data_limit_gb=data_limit_gb,
        expire_days=expire_days,
        key_name=key_name,
    )


def check_user_duplicates(db: Session, telegram_id: int | None, username: str | None, exclude_id: int | None = None) -> None:
    """Проверяет уникальность telegram_id и username. Бросает HTTPException при конфликте."""
    if telegram_id:
        q = db.query(User).filter(User.telegram_id == telegram_id)
        if exclude_id:
            q = q.filter(User.id != exclude_id)
        if q.first():
            raise HTTPException(status_code=409, detail="Пользователь с таким Telegram ID уже существует")
    if username:
        q = db.query(User).filter(User.username == username)
        if exclude_id:
            q = q.filter(User.id != exclude_id)
        if q.first():
            raise HTTPException(status_code=409, detail="Пользователь с таким логином уже существует")


def create_user_and_key(db: Session, data: CreateUserData) -> tuple[User, VPNKey]:
    """Создаёт пользователя с VPN-ключом. Возвращает (user, vpn_key)."""
    check_user_duplicates(db, data.telegram_id, data.username)

    new_user = User(
        telegram_id=data.telegram_id,
        display_name=data.display_name or data.username,
        username=data.username,
        password_hash=hash_password(data.password) if data.password else None,
        sub_token=_gen_uuid(),
        is_active=True,
    )
    db.add(new_user)
    db.flush()

    vpn_key = VPNKey(
        user_id=new_user.id,
        name=data.key_name,
        uuid=generate_uuid(),
        protocol="vless",
        data_limit=int(data.data_limit_gb * GB) if data.data_limit_gb > 0 else None,
        expire_at=datetime.now(timezone.utc) + timedelta(days=data.expire_days) if data.expire_days > 0 else None,
    )
    db.add(vpn_key)
    db.commit()

    return new_user, vpn_key
