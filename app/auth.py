"""Аутентификация: in-memory одноразовые коды + подписанные cookie-сессии + инвайт-ключи + CSRF."""

import logging
import os
import secrets
import threading
import time
from datetime import datetime

from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.models import InviteKey, User

logger = logging.getLogger(__name__)

# ── Настройки ──

CODE_TTL_SECONDS = 300  # 5 минут
SESSION_MAX_AGE = 86400  # 24 часа
CSRF_MAX_AGE = 3600  # 1 час
SECRET_KEY = os.getenv("SECRET_KEY", "")
SESSION_COOKIE = "vpnbzk_session"

if not SECRET_KEY or SECRET_KEY.startswith("change-me"):
    if os.getenv("TESTING"):
        SECRET_KEY = "test-secret-key-not-for-production"
    else:
        raise RuntimeError(
            "SECRET_KEY не задан или содержит значение по умолчанию. "
            "Сгенерируйте: python -c 'import secrets; print(secrets.token_hex(32))'"
        )

_serializer = URLSafeTimedSerializer(SECRET_KEY)
_csrf_serializer = URLSafeTimedSerializer(SECRET_KEY + ":csrf")

# ── In-memory хранилище кодов: {code: (telegram_id, expires_at)} ──

_codes: dict[str, tuple[int, float]] = {}
_codes_lock = threading.Lock()


def _cleanup_expired() -> None:
    """Удаляет просроченные коды (вызывается под блокировкой)."""
    now = time.time()
    expired = [c for c, (_, exp) in _codes.items() if exp <= now]
    for c in expired:
        del _codes[c]


def generate_code(telegram_id: int) -> str:
    """Генерирует 6-значный одноразовый код и сохраняет в памяти."""
    code = str(secrets.randbelow(900000) + 100000)
    expires = time.time() + CODE_TTL_SECONDS
    with _codes_lock:
        # Удаляем предыдущие коды этого пользователя
        stale = [c for c, (tid, _) in _codes.items() if tid == telegram_id]
        for c in stale:
            del _codes[c]
        _cleanup_expired()
        _codes[code] = (telegram_id, expires)
    return code


def verify_code(code: str) -> int | None:
    """Проверяет код. Возвращает telegram_id или None. Код одноразовый."""
    with _codes_lock:
        _cleanup_expired()
        entry = _codes.pop(code, None)
    if entry is None:
        return None
    telegram_id, expires = entry
    if time.time() > expires:
        return None
    return telegram_id


def verify_code_and_get_user(db: Session, code: str) -> User | None:
    """Проверяет код и возвращает User (зарегистрированного и активного) или None."""
    telegram_id = verify_code(code)
    if telegram_id is None:
        return None

    user = db.query(User).filter(User.telegram_id == telegram_id, User.is_active == True).first()  # noqa: E712
    if not user:
        return None

    user.last_login = datetime.utcnow()
    db.commit()
    db.refresh(user)
    return user


# ── Подписанные cookie-сессии ──


def create_session_token(user_id: int) -> str:
    """Создаёт подписанный токен сессии."""
    return _serializer.dumps({"uid": user_id})


def load_session_token(token: str) -> int | None:
    """Возвращает user_id из подписанного токена или None."""
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data.get("uid")
    except (BadSignature, Exception):
        return None


# ── CSRF-токены (подписанные, stateless) ──


def generate_csrf_token() -> str:
    """Генерирует подписанный CSRF-токен (1 час)."""
    return _csrf_serializer.dumps({"nonce": secrets.token_hex(8)})


def validate_csrf_token(token: str | None) -> bool:
    """Проверяет подписанный CSRF-токен."""
    if not token:
        return False
    try:
        _csrf_serializer.loads(token, max_age=CSRF_MAX_AGE)
        return True
    except (BadSignature, Exception):
        return False


# ── Инвайт-ключи ──


def generate_invite_key(db: Session, admin_user_id: int) -> InviteKey:
    """Генерирует уникальный инвайт-ключ (16 символов hex)."""
    key_str = secrets.token_hex(8)
    invite = InviteKey(key=key_str, created_by=admin_user_id)
    db.add(invite)
    db.commit()
    db.refresh(invite)
    return invite


def use_invite_key(db: Session, key: str) -> InviteKey | None:
    """Находит неиспользованный ключ. Возвращает InviteKey или None."""
    invite = db.query(InviteKey).filter(
        InviteKey.key == key,
        InviteKey.is_used == False,  # noqa: E712
    ).first()
    return invite


def mark_invite_used(db: Session, invite: InviteKey, user_id: int) -> None:
    """Помечает ключ как использованный."""
    invite.is_used = True
    invite.used_by = user_id
    invite.used_at = datetime.utcnow()
    db.commit()


def list_invite_keys(db: Session) -> list[InviteKey]:
    """Все инвайт-ключи, по убыванию даты создания."""
    return db.query(InviteKey).order_by(InviteKey.id.desc()).all()


def delete_invite_key(db: Session, key_id: int) -> bool:
    """Удаляет инвайт-ключ. Возвращает True если удалён."""
    invite = db.query(InviteKey).filter(InviteKey.id == key_id).first()
    if not invite:
        return False
    db.delete(invite)
    db.commit()
    return True
