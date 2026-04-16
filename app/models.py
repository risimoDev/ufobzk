"""SQLAlchemy модели для каскадного VPN."""

import os
import uuid as _uuid
from datetime import datetime


def _gen_uuid() -> str:
    return str(_uuid.uuid4())

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

DATABASE_URL = "sqlite:///./data/vpnbzk.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

_sa_raw = os.getenv("SUPERADMIN_TELEGRAM_ID", "0")
try:
    SUPERADMIN_TELEGRAM_ID = int(_sa_raw) if _sa_raw.strip() else 0
except ValueError:
    SUPERADMIN_TELEGRAM_ID = 0


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=True, index=True)
    telegram_username = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    username = Column(String(64), unique=True, nullable=True, index=True)
    password_hash = Column(String(128), nullable=True)
    sub_token = Column(String(36), unique=True, nullable=True, index=True)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

    vpn_keys = relationship("VPNKey", back_populates="user", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="user", cascade="all, delete-orphan", foreign_keys="[Payment.user_id]")


class Payment(Base):
    """История оплат и долгов пользователя."""
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Float, nullable=False, default=0.0)
    currency = Column(String(8), nullable=False, default="RUB")
    status = Column(String(16), nullable=False, default="paid")  # paid | debt | pending
    period_start = Column(DateTime, nullable=True)
    period_end = Column(DateTime, nullable=True)
    note = Column(String(256), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    user = relationship("User", back_populates="payments", foreign_keys=[user_id])


class VPNKey(Base):
    """VPN-ключ. У одного пользователя может быть несколько."""
    __tablename__ = "vpn_keys"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(64), nullable=False, default="default")
    uuid = Column(String(36), unique=True, nullable=False, index=True)
    protocol = Column(String(16), nullable=False, default="vless")
    is_active = Column(Boolean, default=True)
    data_limit = Column(BigInteger, nullable=True)
    data_used = Column(BigInteger, default=0)
    expire_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="vpn_keys")

    @property
    def is_expired(self) -> bool:
        if self.expire_at is None:
            return False
        return datetime.utcnow() > self.expire_at

    @property
    def is_over_limit(self) -> bool:
        if self.data_limit is None:
            return False
        return self.data_used >= self.data_limit

    @property
    def status(self) -> str:
        if not self.is_active:
            return "disabled"
        if self.is_expired:
            return "expired"
        if self.is_over_limit:
            return "limited"
        return "active"


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(64), nullable=False)
    target = Column(String(128), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AppSetting(Base):
    """Настройки приложения, хранимые в БД (редактируются из админки)."""
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def get_setting(db, key: str, default: str = "") -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row and row.value is not None else default


def set_setting(db, key: str, value: str) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


# Default settings (keys expected in DB)
DEFAULT_SETTINGS = {
    "instructions_ios": "1. Установите **Hiddify** из App Store\n2. Нажмите + → «Добавить по ссылке»\n3. Вставьте ссылку подписки или отсканируйте QR-код\n4. Включите подключение",
    "instructions_android": "1. Установите **Hiddify** из Google Play / GitHub\n2. Нажмите «Новый профиль» → «По ссылке»\n3. Вставьте ссылку подписки\n4. Включите VPN",
    "instructions_windows": "1. Скачайте **Hiddify** с github.com/hiddify\n2. «Новый профиль» → «По ссылке»\n3. Вставьте ссылку подписки → «Сохранить»\n4. Подключайтесь",
    "instructions_macos": "1. Скачайте **Hiddify** (.dmg) с GitHub\n2. Добавьте профиль по ссылке подписки\n3. Подключайтесь",
    "app_link_ios": "https://apps.apple.com/app/hiddify-proxy-vpn/id6596777532",
    "app_link_android": "https://play.google.com/store/apps/details?id=app.hiddify.com",
    "app_link_windows": "https://github.com/hiddify/hiddify-app/releases/latest",
    "app_link_macos": "https://github.com/hiddify/hiddify-app/releases/latest",
    "support_text": "По вопросам обратитесь к администратору.",
}


class InviteKey(Base):
    __tablename__ = "invite_keys"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(32), unique=True, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    used_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    is_used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    used_at = Column(DateTime, nullable=True)


def init_db():
    Base.metadata.create_all(bind=engine)
    _db = SessionLocal()
    try:
        # Seed default settings if not present
        for key, value in DEFAULT_SETTINGS.items():
            if not _db.query(AppSetting).filter(AppSetting.key == key).first():
                _db.add(AppSetting(key=key, value=value))
        # Generate sub_tokens for users that don't have one
        for user in _db.query(User).filter(User.sub_token == None).all():  # noqa: E711
            user.sub_token = _gen_uuid()
        _db.commit()
    finally:
        _db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
