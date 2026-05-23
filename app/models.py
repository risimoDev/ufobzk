"""SQLAlchemy модели для каскадного VPN."""

import os
import uuid as _uuid
from datetime import datetime, timezone


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
    event,
)
from sqlalchemy.orm import DeclarativeBase, relationship, sessionmaker

DATABASE_URL = "sqlite:///./data/vpnbzk.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _):
    """WAL mode + оптимизации при каждом открытии соединения с БД."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")    # concurrent reads + writes
    cur.execute("PRAGMA synchronous=NORMAL")  # быстрее, безопасно с WAL
    cur.execute("PRAGMA busy_timeout=30000")  # 30 сек ожидание вместо ошибки
    cur.execute("PRAGMA cache_size=-32000")   # 32 MB page cache
    cur.execute("PRAGMA temp_store=MEMORY")   # temp-таблицы в RAM
    cur.close()


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
    sub_token_updated_at = Column(DateTime, nullable=True)
    is_admin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    is_free = Column(Boolean, default=False)  # Бесплатный доступ на всё время
    free_reason = Column(String, nullable=True)  # Причина бесплатного доступа
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_login = Column(DateTime, nullable=True)

    vpn_keys = relationship("VPNKey", back_populates="user", cascade="all, delete-orphan")
    payments = relationship("Payment", back_populates="user", cascade="all, delete-orphan", foreign_keys="[Payment.user_id]")


class Server(Base):
    """Remote Xray node server."""
    __tablename__ = "servers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), nullable=False)
    host = Column(String(255), nullable=False)
    api_url = Column(String(255), nullable=False)
    api_token = Column(String(128), nullable=False)
    region = Column(String(16), nullable=False, default="eu")
    role = Column(String(16), nullable=False, default="remote")
    is_active = Column(Boolean, default=True)
    reality_public_key = Column(String(128), nullable=True)
    reality_private_key = Column(String(128), nullable=True)
    reality_dest = Column(String(128), nullable=True)
    reality_server_names = Column(String(255), nullable=True)
    reality_short_id = Column(String(32), nullable=True)
    ws_port = Column(Integer, nullable=False, default=443)
    reality_port = Column(Integer, nullable=False, default=2053)
    grpc_port = Column(Integer, nullable=False, default=8445)
    xhttp_port = Column(Integer, nullable=False, default=8444)
    priority = Column(Integer, nullable=False, default=100)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_sync = Column(DateTime, nullable=True)
    last_sync_status = Column(String(16), nullable=True)


class InviteKey(Base):
    """Инвайт-ключ для регистрации новых пользователей."""
    __tablename__ = "invite_keys"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    is_used = Column(Boolean, default=False)
    used_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)

    used_by_user = relationship("User", foreign_keys=[used_by], backref="used_invites")
    creator = relationship("User", foreign_keys=[created_by], backref="created_invites")


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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
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
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="vpn_keys")

    @property
    def is_expired(self) -> bool:
        if self.expire_at is None:
            return False
        return datetime.now(timezone.utc) > self.expire_at

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


class TrafficSnapshot(Base):
    """Ежечасные снимки трафика для построения графиков и аналитики."""
    __tablename__ = "traffic_snapshots"

    id = Column(Integer, primary_key=True)
    vpn_key_id = Column(
        Integer,
        ForeignKey("vpn_keys.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    bytes_delta = Column(BigInteger, default=0)   # прирост за период
    total_bytes = Column(BigInteger, default=0)   # кумулятив на момент записи
    recorded_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    vpn_key = relationship("VPNKey", backref="snapshots")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(64), nullable=False)
    target = Column(String(128), nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Guide(Base):
    """Гайды / инструкции по подключению для пользователей."""
    __tablename__ = "guides"

    id = Column(Integer, primary_key=True, index=True)
    slug = Column(String(64), unique=True, nullable=False, index=True)
    title = Column(String(128), nullable=False)
    app_name = Column(String(64), nullable=False)
    platform = Column(String(16), nullable=False, default="all")
    content = Column(Text, nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class AppSetting(Base):
    """Настройки приложения, хранимые в БД (редактируются из админки)."""
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


def get_setting(db, key: str, default: str = "") -> str:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row and row.value is not None else default


def set_setting(db, key: str, value: str) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(AppSetting(key=key, value=value))
    db.commit()


DEFAULT_GUIDES = [
    {
        "slug": "hiddify-all",
        "title": "Hiddify — универсальный клиент",
        "app_name": "Hiddify",
        "platform": "all",
        "sort_order": 1,
        "content": (
            "<h3>1. Установите Hiddify</h3>"
            "<ul>"
            "<li><b>iOS:</b> App Store → <i>Hiddify Proxy & VPN</i></li>"
            "<li><b>Android:</b> Google Play или GitHub releases</li>"
            "<li><b>Windows/macOS:</b> github.com/hiddify/hiddify-app/releases</li>"
            "</ul>"
            "<h3>2. Получите ссылку подписки</h3>"
            "<p>В личном кабинете нажмите <b>«Копировать ссылку подписки»</b> или отсканируйте QR-код.</p>"
            "<h3>3. Добавьте профиль</h3>"
            "<ul>"
            "<li>Откройте Hiddify → нажмите <b>+</b> или <b>«Новый профиль»</b></li>"
            "<li>Выберите <b>«Добавить по ссылке»</b></li>"
            "<li>Вставьте ссылку подписки → <b>Сохранить</b></li>"
            "</ul>"
            "<h3>4. Подключитесь</h3>"
            "<p>Выберите добавленный профиль и нажмите кнопку подключения. Если не работает — попробуйте сменить протокол в настройках профиля (VLESS/Reality или WS).</p>"
        ),
    },
    {
        "slug": "v2rayng-android",
        "title": "V2RayNG — Android",
        "app_name": "V2RayNG",
        "platform": "android",
        "sort_order": 2,
        "content": (
            "<h3>1. Установите V2RayNG</h3>"
            "<p>Google Play (если доступен) или GitHub: github.com/2dust/v2rayNG/releases — скачайте APK.</p>"
            "<h3>2. Получите конфигурацию</h3>"
            "<p>В личном кабинете скопируйте <b>ссылку подписки</b> или QR-код.</p>"
            "<h3>3. Импорт конфига</h3>"
            "<ul>"
            "<li>Откройте V2RayNG → нажмите <b>+</b> вверху справа</li>"
            "<li>Выберите <b>«Импорт из буфера обмена»</b> (если скопировали ссылку)</li>"
            "<li>Или <b>«Импорт по QR-коду»</b> → отсканируйте QR из кабинета</li>"
            "</ul>"
            "<h3>4. Запуск</h3>"
            "<p>Выберите добавленный сервер → нажмите <b>V</b> внизу экрана. При первом запуске разрешите VPN-соединение.</p>"
            "<h3>Совет</h3>"
            "<p>Если соединение не устанавливается: зайдите в настройки профиля → включите <b>MUX</b> или смените <b>Security</b> на <b>Reality/WS</b> в зависимости от сервера.</p>"
        ),
    },
    {
        "slug": "streisand-ios",
        "title": "Streisand — iOS",
        "app_name": "Streisand",
        "platform": "ios",
        "sort_order": 3,
        "content": (
            "<h3>1. Установите Streisand</h3>"
            "<p>App Store → найдите <i>Streisand</i> (бесплатный, без рекламы).</p>"
            "<h3>2. Получите ссылку или QR</h3>"
            "<p>В личном кабинете скопируйте <b>ссылку подписки</b> (vless://...) или отсканируйте QR-код.</p>"
            "<h3>3. Добавьте профиль</h3>"
            "<ul>"
            "<li>Streisand → <b>+</b> → <b>«Добавить из буфера»</b></li>"
            "<li>Или <b>«Сканировать QR»</b></li>"
            "</ul>"
            "<h3>4. Подключение</h3>"
            "<p>Нажмите на добавленный профиль → переключите тумблер <b>VPN</b>. В iOS может появиться запрос на разрешение конфигурации — нажмите <b>Разрешить</b>.</p>"
            "<h3>Если не подключается</h3>"
            "<p>Проверьте в настройках профиля:</p>"
            "<ul>"
            "<li><b>Network:</b> ws или reality (спросите админа)</li>"
            "<li><b>TLS:</b> включён, SNI совпадает с dest</li>"
            "</ul>"
        ),
    },
    {
        "slug": "shadowrocket-ios",
        "title": "Shadowrocket — iOS",
        "app_name": "Shadowrocket",
        "platform": "ios",
        "sort_order": 4,
        "content": (
            "<h3>1. Установите Shadowrocket</h3>"
            "<p>App Store — платное приложение (~$3). Альтернатива: Streisand (бесплатно).</p>"
            "<h3>2. Добавьте конфиг</h3>"
            "<ul>"
            "<li>Скопируйте <b>ссылку подписки</b> из личного кабинета</li>"
            "<li>Shadowrocket → <b>+</b> → <b>«Добавить из буфера»</b></li>"
            "</ul>"
            "<h3>3. Подключение</h3>"
            "<p>Включите главный тумблер. При первом запуске iOS запросит добавление VPN-конфигурации — разрешите.</p>"
            "<h3>Настройка</h3>"
            "<p>Для Reality-протокола убедитесь, что в профиле указаны:</p>"
            "<ul>"
            "<li><b>Public Key</b> — из кабинета</li>"
            "<li><b>Short ID</b> — из кабинета</li>"
            "<li><b>SpiderX / SNI</b> — совпадает с dest</li>"
            "</ul>"
        ),
    },
    {
        "slug": "v2rayn-windows",
        "title": "V2RayN — Windows",
        "app_name": "V2RayN",
        "platform": "windows",
        "sort_order": 5,
        "content": (
            "<h3>1. Установите V2RayN</h3>"
            "<p>github.com/2dust/v2rayN/releases — скачайте <b>v2rayN-With-Core.zip</b>, распакуйте в любую папку.</p>"
            "<h3>2. Запуск</h3>"
            "<p>Запустите <b>v2rayN.exe</b>. При первом запуске может потребоваться установка .NET Runtime — следуйте подсказкам.</p>"
            "<h3>3. Импорт</h3>"
            "<ul>"
            "<li>Скопируйте <b>ссылку подписки</b> из личного кабинета</li>"
            "<li>V2RayN → <b>Серверы</b> → <b>Импорт из буфера обмена</b></li>"
            "<li>Или <b>QR-код</b> → <b>Сканировать экран</b></li>"
            "</ul>"
            "<h3>4. Подключение</h3>"
            "<p>Выберите сервер в списке → <b>Ctrl+R</b> или правый клик → <b>Установить как активный</b>. Затем нажмите <b>Запустить</b> (Ctrl+S).</p>"
            "<h3>Режим работы</h3>"
            "<p>В трее правый клик по иконке V2RayN → <b>Режим прокси</b>:</p>"
            "<ul>"
            "<li><b>Автоматическая конфигурация (PAC)</b> — только заблокированные сайты через VPN</li>"
            "<li><b>Глобальный прокси</b> — весь трафик через VPN</li>"
            "</ul>"
        ),
    },
    {
        "slug": "nekoray-windows",
        "title": "Nekoray — Windows / Linux",
        "app_name": "Nekoray",
        "platform": "windows",
        "sort_order": 6,
        "content": (
            "<h3>1. Установите Nekoray</h3>"
            "<p>github.com/MatsuriDayo/nekoray/releases — скачайте zip для Windows или AppImage для Linux.</p>"
            "<h3>2. Запуск</h3>"
            "<p>Распакуйте и запустите <b>nekoray.exe</b>. На Linux дайте права на выполнение AppImage.</p>"
            "<h3>3. Добавьте профиль</h3>"
            "<ul>"
            "<li><b>Программа</b> → <b>Новый профиль</b> → <b>Импорт</b> → <b>Из буфера обмена</b></li>"
            "<li>Или <b>Ручной ввод</b> → протокол <b>VLESS</b></li>"
            "</ul>"
            "<h3>4. Параметры Reality</h3>"
            "<p>Если используете Reality-протокол, вручную укажите:</p>"
            "<ul>"
            "<li><b>Public Key</b>, <b>Short ID</b> — из кабинета</li>"
            "<li><b>Flow:</b> xtls-rprx-vision</li>"
            "<li><b>Dest/SNI:</b> www.samsung.com:443</li>"
            "</ul>"
            "<h3>5. Запуск</h3>"
            "<p>Выберите профиль → <b>Запустить</b>. Убедитесь, что в трее появилась иконка с зелёной галочкой.</p>"
        ),
    },
    {
        "slug": "foxray-ios",
        "title": "Foxray — iOS (альтернатива)",
        "app_name": "Foxray",
        "platform": "ios",
        "sort_order": 7,
        "content": (
            "<h3>1. Установите Foxray</h3>"
            "<p>App Store → <i>Foxray</i> — бесплатный клиент с поддержкой Xray-core.</p>"
            "<h3>2. Импорт</h3>"
            "<ul>"
            "<li>Скопируйте <b>ссылку подписки</b> из кабинета</li>"
            "<li>Foxray → <b>+</b> → <b>Import from clipboard</b></li>"
            "</ul>"
            "<h3>3. Подключение</h3>"
            "<p>Нажмите на импортированный профиль → <b>Connect</b>. Разрешите добавление VPN-конфигурации.</p>"
            "<h3>Совет</h3>"
            "<p>Foxray автоматически определяет Reality-параметры из ссылки. Если что-то не работает — проверьте вручную поля <b>PublicKey</b> и <b>ShortId</b> в настройках профиля.</p>"
        ),
    },
    {
        "slug": "karing-all",
        "title": "Karing — кроссплатформенный клиент",
        "app_name": "Karing",
        "platform": "all",
        "sort_order": 8,
        "content": (
            "<h3>1. Установите Karing</h3>"
            "<ul>"
            "<li><b>Android:</b> GitHub releases (github.com/KaringX/karing) — скачайте APK или используйте Obtainium</li>"
            "<li><b>iOS:</b> TestFlight или App Store (если доступен в регионе)</li>"
            "<li><b>Windows/macOS:</b> GitHub releases — скачайте установщик</li>"
            "</ul>"
            "<h3>2. Импорт подписки</h3>"
            "<p>Скопируйте <b>ссылку подписки</b> из личного кабинета.</p>"
            "<ul>"
            "<li>Karing → <b>Профили</b> → <b>+</b> → <b>Добавить по URL</b></li>"
            "<li>Вставьте ссылку → <b>ОК</b></li>"
            "</ul>"
            "<h3>3. Выбор профиля и подключение</h3>"
            "<p>Выберите импортированный профиль → нажмите большую кнопку <b>Подключить</b>. Разрешите создание VPN.</p>"
            "<h3>Особенности</h3>"
            "<ul>"
            "<li>Встроенные правила маршрутизации (geoip/geosite)</li>"
            "<li>Поддержка TUN-режима на Windows/macOS</li>"
            "<li>Автообновление подписки</li>"
            "</ul>"
        ),
    },
    {
        "slug": "nekobox-android",
        "title": "NekoBox for Android",
        "app_name": "NekoBox",
        "platform": "android",
        "sort_order": 9,
        "content": (
            "<h3>1. Установите NekoBox</h3>"
            "<p>GitHub: github.com/MatsuriDayo/NekoBoxForAndroid/releases — скачайте APK (обычно <b>arm64-v8a</b>).</p>"
            "<h3>2. Импорт подписки</h3>"
            "<ul>"
            "<li>Скопируйте <b>ссылку подписки</b> из кабинета</li>"
            "<li>NekoBox → <b>Группы</b> → <b>+</b> → <b>URL</b></li>"
            "<li>Вставьте ссылку, задайте имя → <b>ОК</b></li>"
            "<li>Нажмите <b>Обновить</b> на группе</li>"
            "</ul>"
            "<h3>3. Подключение</h3>"
            "<p>Выберите сервер → нажмите плавающую кнопку <b>▶</b>. При первом запуске разрешите VPN.</p>"
            "<h3>Режим работы</h3>"
            "<p>Настройки → <b>Режим работы</b>:</p>"
            "<ul>"
            "<li><b>VPN</b> — весь трафик через туннель (TUN)</li>"
            "<li><b>Только прокси</b> — локальный SOCKS5/HTTP</li>"
            "</ul>"
            "<h3>Совет</h3>"
            "<p>Если Reality не работает: вручную проверьте <b>Public Key</b>, <b>Short ID</b> и <b>SNI</b> в настройках узла.</p>"
        ),
    },
    {
        "slug": "potatso-ios",
        "title": "Potatso — iOS",
        "app_name": "Potatso",
        "platform": "ios",
        "sort_order": 10,
        "content": (
            "<h3>1. Установите Potatso</h3>"
            "<p>App Store → <i>Potatso 2</i> или <i>Potatso Lite</i> (платное/условно-бесплатное приложение).</p>"
            "<h3>2. Импорт конфигурации</h3>"
            "<ul>"
            "<li>Скопируйте <b>ссылку подписки</b> из кабинета</li>"
            "<li>Potatso → <b>+</b> → <b>Import from Clipboard</b></li>"
            "<li>Или отсканируйте <b>QR-код</b></li>"
            "</ul>"
            "<h3>3. Подключение</h3>"
            "<p>Выберите профиль → переключите тумблер <b>VPN</b>. В iOS подтвердите добавление конфигурации.</p>"
            "<h3>Настройка маршрутизации</h3>"
            "<p>Potatso поддерживает PAC и правила по доменам. Для простоты включите <b>Global Routing</b> или импортируйте правила.</p>"
        ),
    },
    {
        "slug": "quantumultx-ios",
        "title": "Quantumult X — iOS (продвинутый)",
        "app_name": "Quantumult X",
        "platform": "ios",
        "sort_order": 11,
        "content": (
            "<h3>1. Установите Quantumult X</h3>"
            "<p>App Store — платное приложение (~$8-10). Один из самых мощных клиентов для iOS.</p>"
            "<h3>2. Импорт подписки</h3>"
            "<ul>"
            "<li>Скопируйте <b>ссылку подписки</b> из кабинета</li>"
            "<li>Quantumult X → вкладка <b>Settings</b> → <b>Profiles</b> → <b>+</b></li>"
            "<li>Вставьте ссылку в поле <b>Remote</b></li>"
            "</ul>"
            "<h3>3. Активация</h3>"
            "<p>Вернитесь на вкладку <b>Home</b> → переключите тумблер <b>VPN</b>. Разрешите добавление конфигурации.</p>"
            "<h3>Особенности</h3>"
            "<ul>"
            "<li>Мощные правила маршрутизации (filter + policy)</li>"
            "<li>Поддержка JavaScript-обработки трафика</li>"
            "<li>Reality, gRPC, WS полностью поддерживаются</li>"
            "</ul>"
            "<h3>Совет</h3>"
            "<p>Для Reality убедитесь, что в профиле указаны <b>Public Key</b> и <b>Short ID</b>. Quantumult X иногда требует ручной правки JSON конфигурации для редких параметров.</p>"
        ),
    },
    {
        "slug": "singbox-all",
        "title": "Sing-box — официальный клиент",
        "app_name": "Sing-box",
        "platform": "all",
        "sort_order": 12,
        "content": (
            "<h3>1. Установите Sing-box</h3>"
            "<ul>"
            "<li><b>iOS:</b> App Store → <i>SFM (sing-box for Mobile)</i></li>"
            "<li><b>Android:</b> Google Play → <i>sing-box</i> или APK с GitHub</li>"
            "<li><b>Windows/macOS/Linux:</b> github.com/SagerNet/sing-box/releases</li>"
            "</ul>"
            "<h3>2. Импорт подписки</h3>"
            "<ul>"
            "<li>Скопируйте <b>ссылку подписки</b> из кабинета</li>"
            "<li>Sing-box → <b>Профили</b> → <b>+</b> → <b>Remote</b></li>"
            "<li>Вставьте URL → задайте имя → <b>Сохранить</b></li>"
            "<li>Нажмите <b>Обновить</b> на профиле</li>"
            "</ul>"
            "<h3>3. Подключение</h3>"
            "<p>Выберите профиль → нажмите кнопку <b>Connect</b>. Разрешите VPN-соединение.</p>"
            "<h3>Особенности</h3>"
            "<ul>"
            "<li>Официальный клиент для sing-box core</li>"
            "<li>Поддержка TUN, WireGuard, Reality, gRPC, Hysteria2</li>"
            "<li>Минималистичный интерфейс, стабильная работа</li>"
            "</ul>"
        ),
    },
]


def _seed_guides(db):
    for g in DEFAULT_GUIDES:
        if not db.query(Guide).filter(Guide.slug == g["slug"]).first():
            db.add(Guide(
                slug=g["slug"],
                title=g["title"],
                app_name=g["app_name"],
                platform=g["platform"],
                content=g["content"],
                sort_order=g["sort_order"],
                is_active=True,
            ))


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


def init_db():
    Base.metadata.create_all(bind=engine)
    _db = SessionLocal()
    try:
        try:
            # Seed default settings if not present
            for key, value in DEFAULT_SETTINGS.items():
                if not _db.query(AppSetting).filter(AppSetting.key == key).first():
                    _db.add(AppSetting(key=key, value=value))
            # Generate sub_tokens for users that don't have one
            for user in _db.query(User).filter(User.sub_token == None).all():  # noqa: E711
                user.sub_token = _gen_uuid()
            # Seed default guides if none exist
            if not _db.query(Guide).first():
                _seed_guides(_db)
            _db.commit()
        except Exception as e:
            _db.rollback()
            import logging as _logging
            _logging.getLogger(__name__).warning("init_db seeding skipped (likely schema mismatch): %s", e)
    finally:
        _db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
