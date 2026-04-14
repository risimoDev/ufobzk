"""SQLAlchemy модели для каскадного VPN."""

import os
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
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

_sa_raw = os.getenv("SUPERADMIN_TELEGRAM_ID", "")
if not _sa_raw:
    raise RuntimeError(
        "SUPERADMIN_TELEGRAM_ID не задан в .env. "
        "Укажите Telegram ID суперадмина."
    )
SUPERADMIN_TELEGRAM_ID = int(_sa_raw)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    telegram_username = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime, nullable=True)

    vpn_keys = relationship("VPNKey", back_populates="user", cascade="all, delete-orphan")


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


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
