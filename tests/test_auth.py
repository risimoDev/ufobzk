from app.auth import (
    hash_password,
    verify_password,
    create_session_token,
    load_session_token,
    generate_csrf_token,
    validate_csrf_token,
    create_magic_token,
    load_magic_token,
    generate_code,
    verify_code_and_get_user,
    generate_tg_link_code,
    verify_tg_link_code,
    create_tg_link_token,
    load_tg_link_token,
    generate_invite_key,
    use_invite_key,
    mark_invite_used,
)

def test_password_hashing():
    pw = "superSecretPassword123!"
    h = hash_password(pw)
    assert h != pw
    assert verify_password(pw, h) is True
    assert verify_password("wrongPassword", h) is False
    assert verify_password("", h) is False


def test_session_tokens():
    token = create_session_token(42)
    assert isinstance(token, str)
    user_id = load_session_token(token)
    assert user_id == 42

    # Invalid token
    assert load_session_token("invalid.token.here") is None
    assert load_session_token("") is None


def test_csrf_tokens():
    token = generate_csrf_token()
    assert isinstance(token, str)
    assert validate_csrf_token(token) is True
    assert validate_csrf_token("invalid_csrf_token") is False
    assert validate_csrf_token(None) is False
    assert validate_csrf_token("") is False


def test_magic_tokens():
    token = create_magic_token(101)
    assert isinstance(token, str)
    assert load_magic_token(token) == 101
    assert load_magic_token("tampered_magic_token") is None


def test_telegram_one_time_code(db_session, sample_user):
    code = generate_code(sample_user.telegram_id)
    assert len(code) == 6
    assert code.isdigit()

    # Verify code succeeds
    found_user = verify_code_and_get_user(db_session, code)
    assert found_user is not None
    assert found_user.id == sample_user.id

    # Code is single-use, should fail second time
    assert verify_code_and_get_user(db_session, code) is None

    # Unknown code
    assert verify_code_and_get_user(db_session, "999999") is None


def test_tg_link_code_and_token():
    code = generate_tg_link_code(55)
    assert len(code) == 6
    assert verify_tg_link_code(code) == 55
    # Single-use check
    assert verify_tg_link_code(code) is None

    token = create_tg_link_token(55)
    assert load_tg_link_token(token) == 55
    assert load_tg_link_token("corrupted") is None


def test_invite_keys(db_session, sample_admin, sample_user):
    invite = generate_invite_key(db_session, sample_admin.id)
    assert invite.key is not None
    assert invite.is_used is False

    # Check using invite key
    found = use_invite_key(db_session, invite.key)
    assert found is not None
    assert found.id == invite.id

    # Mark as used
    mark_invite_used(db_session, found, sample_user.id)
    assert found.is_used is True
    assert found.used_by == sample_user.id
    assert found.used_at is not None

    # Trying to reuse used key should fail
    assert use_invite_key(db_session, invite.key) is None
