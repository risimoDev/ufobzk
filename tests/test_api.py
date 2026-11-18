import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.models import get_db


@pytest.fixture
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_robots_txt(client):
    resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert "Disallow: /" in resp.text


def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["db"] == "ok"


def test_security_middleware_blocked_ua(client):
    resp = client.get("/", headers={"User-Agent": "sqlmap/1.5#dev"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Forbidden"


def test_security_middleware_blocked_path(client):
    resp = client.get("/wp-admin/index.php")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not Found"


def test_security_headers(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers.get("x-frame-options") == "DENY"
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" in resp.headers


def test_subscription_endpoint(client, sample_user):
    token = sample_user.sub_token
    resp = client.get(f"/sub/{token}")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert "Subscription-Userinfo" in resp.headers
    assert len(resp.text) > 0


def test_subscription_json_endpoint(client, sample_user):
    token = sample_user.sub_token
    resp = client.get(f"/sub/json/{token}")
    assert resp.status_code == 200
    data = resp.json()
    assert "outbounds" in data
    assert "routing" in data


def test_subscription_invalid_token(client):
    resp = client.get("/sub/non-existent-token-12345")
    assert resp.status_code == 404


def test_login_invalid_credentials(client):
    from app.auth import generate_csrf_token
    token = generate_csrf_token()
    resp = client.post(
        "/login",
        data={"username": "wronguser", "password": "wrongpassword", "csrf_token": token},
        headers={"x-csrf-token": token},
    )
    assert resp.status_code == 200
    assert "Неверный логин/пароль или код" in resp.text
