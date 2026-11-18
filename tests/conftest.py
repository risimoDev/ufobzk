import os

# Ensure testing mode is active
os.environ["TESTING"] = "1"
os.environ["SECRET_KEY"] = "test-secret-key-for-testing-purposes-only-32bytes"
os.environ["DOMAIN"] = "test.ufolabs.online"
os.environ["NL_SERVER_IP"] = "193.47.60.232"
os.environ["RU_SERVER_IP"] = "194.226.54.42"
os.environ["REALITY_PUBLIC_KEY"] = "NG63mjKr8RNFKeJSEyoqOBW_UwPos8YGpqbkfamc60M"
os.environ["REALITY_PRIVATE_KEY"] = "uMtnwXWJdA3yicCZql_HFbp2V8CEYChUJJBhOHvbVXI"
os.environ["REALITY_SHORT_ID"] = "c21c6ddc00a0d49b"
os.environ["REALITY_DEST"] = "github.com:443"
os.environ["REALITY_SERVER_NAMES"] = "github.com,www.github.com"
os.environ["RU_TRANSIT_UUID"] = "11111111-1111-1111-1111-111111111111"
os.environ["RU_TRANSIT_PUBLIC_KEY"] = "ru_public_key_test_12345"
os.environ["RU_TRANSIT_SHORT_ID"] = "aabbccdd"
os.environ["RU_TRANSIT_SN"] = "www.google.com"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Server, User, VPNKey
from app.auth import hash_password

TEST_DB_URL = "sqlite:///:memory:"

test_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sample_user(db_session):
    user = User(
        username="testuser",
        password_hash=hash_password("testpass123"),
        telegram_id=12345678,
        display_name="Test User",
        sub_token="sub-token-uuid-12345",
        is_active=True,
        is_admin=False,
    )
    db_session.add(user)
    db_session.flush()

    key = VPNKey(
        user_id=user.id,
        name="test-key",
        uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        protocol="vless",
        is_active=True,
        data_used=1024 * 1024 * 50,  # 50 MB
        data_limit=1024 * 1024 * 1024,  # 1 GB
    )
    db_session.add(key)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def sample_admin(db_session):
    admin = User(
        username="admin",
        password_hash=hash_password("adminpass123"),
        telegram_id=99999999,
        display_name="Admin User",
        sub_token="admin-token-uuid-99999",
        is_active=True,
        is_admin=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


@pytest.fixture
def sample_server(db_session):
    server = Server(
        name="Node-Helsinki",
        region="fi",
        host="95.217.100.100",
        api_url="http://95.217.100.100:9090",
        api_token="test_api_token_hex_64",
        role="direct",
        is_active=True,
        reality_port=443,
        reality_public_key="node_pub_key_123",
        reality_private_key="node_priv_key_123",
        reality_short_id="1234abcd",
        hysteria_port=8443,
        tuic_port=8444,
        core_type="sing-box",
        is_blocked_ru=False,
    )
    db_session.add(server)
    db_session.commit()
    db_session.refresh(server)
    return server
