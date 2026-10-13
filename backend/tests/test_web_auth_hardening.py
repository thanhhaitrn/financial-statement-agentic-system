"""Login/sign-up rate limits, server-side logout, invites and safe 422 bodies."""

import ipaddress
from dataclasses import replace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("jwt")

from fastapi.testclient import TestClient  # noqa: E402

from agentfinx.api import auth, throttle  # noqa: E402
from agentfinx.api import cli as webcli  # noqa: E402
from agentfinx.api import db as webdb  # noqa: E402
from agentfinx.api.app import create_app  # noqa: E402
from agentfinx.api.settings import WebConfigError, WebSettings, load_settings  # noqa: E402

ORIGIN = "http://127.0.0.1:8000"
SECRET = "hardening-secret-" + "h" * 40
PASSWORD = "correct horse battery"


def _app(tmp_path, **overrides):
    settings = WebSettings(secret_key=SECRET, db_path=tmp_path / "web.sqlite3",
                           allowed_origins=frozenset({ORIGIN}), registration_mode="open")
    return create_app(replace(settings, **overrides))


def _client(app):
    return TestClient(app, base_url=ORIGIN)


def _post(client, path, payload, headers=None):
    return client.post(path, json=payload, headers={"Origin": ORIGIN, **(headers or {})})


def _register(client, email, **extra):
    return _post(client, "/api/register", {"name": "Người Dùng", "email": email, "password": PASSWORD, **extra})


def _invite(app, email, *, days=7):
    from datetime import datetime, timedelta, timezone

    code = auth.new_invite_code()
    with app.state.db.transaction() as conn:
        invite = webdb.create_invite(conn, code_hash=auth.hash_invite_code(code), email=email,
                                     expires_at=webdb.iso(datetime.now(timezone.utc) + timedelta(days=days)))
    return code, invite["id"]


@pytest.fixture
def count_argon2(monkeypatch):
    calls = {"verify": 0, "hash": 0}
    verify, hash_ = auth.verify_password, auth.hash_password

    def counting_verify(*args):
        calls["verify"] += 1
        return verify(*args)

    def counting_hash(*args):
        calls["hash"] += 1
        return hash_(*args)

    monkeypatch.setattr(auth, "verify_password", counting_verify)
    monkeypatch.setattr(auth, "hash_password", counting_hash)
    return calls


# ------------------------------------------------------------------ login limits


def test_failed_logins_lock_the_account_before_any_password_check(tmp_path, count_argon2):
    app = _app(tmp_path)
    client = _client(app)
    assert _register(client, "alice@example.com").status_code == 201
    for _ in range(5):
        wrong = _post(client, "/api/login", {"email": "alice@example.com", "password": "wrong-password"})
        assert wrong.status_code == 401
    verified = count_argon2["verify"]

    locked = _post(client, "/api/login", {"email": "Alice@Example.com", "password": PASSWORD})
    assert locked.status_code == 429
    assert locked.json()["detail"]["code"] == "too_many_attempts"
    assert 0 < int(locked.headers["retry-after"]) <= 15 * 60
    assert count_argon2["verify"] == verified  # refused before Argon2

    # Unknown accounts are limited the same way: the 429 reveals nothing.
    for _ in range(5):
        _post(client, "/api/login", {"email": "ghost@example.com", "password": "wrong-password"})
    ghost = _post(client, "/api/login", {"email": "ghost@example.com", "password": "wrong-password"})
    assert ghost.status_code == 429 and ghost.json() == locked.json()


def test_a_successful_login_clears_the_account_failures(tmp_path):
    client = _client(_app(tmp_path))
    _register(client, "bob@example.com")
    for _ in range(4):
        _post(client, "/api/login", {"email": "bob@example.com", "password": "wrong-password"})
    assert _post(client, "/api/login", {"email": "bob@example.com", "password": PASSWORD}).status_code == 200
    for _ in range(4):
        assert _post(client, "/api/login", {"email": "bob@example.com", "password": "nope-nope"}).status_code == 401


def test_one_address_cannot_spray_passwords_across_accounts(tmp_path):
    client = _client(_app(tmp_path, login_ip_limit=3))
    for index in range(3):
        response = _post(client, "/api/login", {"email": f"user{index}@example.com", "password": "guess-1234"})
        assert response.status_code == 401
    sprayed = _post(client, "/api/login", {"email": "user9@example.com", "password": "guess-1234"})
    assert sprayed.status_code == 429


def test_sliding_window_and_retry_after(tmp_path):
    database = webdb.WebDB(tmp_path / "web.sqlite3")
    database.migrate()
    limit = throttle.Limit("login_account", 2, 60)
    with database.transaction() as conn:
        throttle.admit(conn, [(limit, "a")], now=1000.0)
        throttle.admit(conn, [(limit, "a")], now=1030.0)
        with pytest.raises(throttle.Throttled) as refused:
            throttle.admit(conn, [(limit, "a")], now=1040.0)
        assert refused.value.retry_after == 21  # the attempt at t=1000 leaves at t=1060
        throttle.admit(conn, [(limit, "a")], now=1061.0)
        throttle.admit(conn, [(limit, "b")], now=1061.0)  # other keys are independent
        stored = [row["key_hash"] for row in conn.execute("SELECT key_hash FROM auth_attempts")]
    assert "a" not in stored and all(len(item) == 64 for item in stored)  # keys stored hashed


# ---------------------------------------------------------------- sign-up limits


def test_sign_up_attempts_are_limited_before_hashing(tmp_path, count_argon2):
    client = _client(_app(tmp_path, register_ip_limit=2))
    assert _register(client, "one@example.com").status_code == 201
    assert _register(client, "two@example.com").status_code == 201
    hashed = count_argon2["hash"]
    spam = _register(client, "three@example.com")
    assert spam.status_code == 429
    assert count_argon2["hash"] == hashed


def test_client_address_trusts_forwarded_for_only_from_listed_proxies():
    loopback = (ipaddress.ip_network("127.0.0.1/32"),)
    assert throttle.client_address("203.0.113.9", "6.6.6.6", ()) == "203.0.113.9"
    assert throttle.client_address("127.0.0.1", None, ()) == "127.0.0.1"
    # Behind a trusted proxy the right-most untrusted hop is the client; a
    # value the client wrote on the left is ignored.
    assert throttle.client_address("127.0.0.1", "6.6.6.6, 198.51.100.7", loopback) == "198.51.100.7"
    assert throttle.client_address("203.0.113.9", "6.6.6.6", loopback) == "203.0.113.9"
    with pytest.raises(WebConfigError):
        load_settings({"FINX_SECRET_KEY": SECRET, "FINX_TRUSTED_PROXIES": "not-an-ip"})
    settings = load_settings({"FINX_SECRET_KEY": SECRET, "FINX_TRUSTED_PROXIES": "127.0.0.1, ::1"})
    assert len(settings.trusted_proxies) == 2


def test_trusted_proxy_limits_each_client_separately(tmp_path):
    app = _app(tmp_path, register_ip_limit=1, trusted_proxies=(ipaddress.ip_network("127.0.0.1/32"),))
    client = TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000))  # the proxy's socket
    first = _post(client, "/api/register", {"name": "A", "email": "a1@example.com", "password": PASSWORD},
                  headers={"X-Forwarded-For": "198.51.100.1"})
    second = _post(client, "/api/register", {"name": "B", "email": "b1@example.com", "password": PASSWORD},
                   headers={"X-Forwarded-For": "198.51.100.2"})
    again = _post(client, "/api/register", {"name": "C", "email": "c1@example.com", "password": PASSWORD},
                  headers={"X-Forwarded-For": "198.51.100.1"})
    assert (first.status_code, second.status_code, again.status_code) == (201, 201, 429)


# ------------------------------------------------------------------ logout


def test_logout_revokes_the_token_itself(tmp_path):
    app = _app(tmp_path)
    device_a, device_b = _client(app), _client(app)
    _register(device_a, "carol@example.com")
    stolen = device_a.cookies.get(auth.SESSION_COOKIE)
    assert _post(device_b, "/api/login", {"email": "carol@example.com", "password": PASSWORD}).status_code == 200

    assert _post(device_a, "/api/logout", {}).status_code == 200
    replay = _client(app)
    replay.cookies.set(auth.SESSION_COOKIE, stolen)
    assert replay.get("/api/me").status_code == 401
    assert device_b.get("/api/me").status_code == 200  # other logins stay valid


def test_tokens_without_a_live_server_session_are_rejected(tmp_path):
    import jwt as pyjwt

    app = _app(tmp_path)
    client = _client(app)
    user = _register(client, "dave@example.com").json()["user"]
    legacy = pyjwt.encode({"sub": user["id"], "typ": "session", "iat": 1, "exp": 4102444800},
                          SECRET, algorithm="HS256")
    unknown = auth.issue_token(user["id"], "no-such-session", SECRET, 3600)
    for token in (legacy, unknown):
        probe = _client(app)
        probe.cookies.set(auth.SESSION_COOKIE, token)
        assert probe.get("/api/me").status_code == 401


# ------------------------------------------------------------------ invites


def test_invite_mode_needs_a_matching_unused_invite(tmp_path, count_argon2):
    app = _app(tmp_path, registration_mode="invite")
    client = _client(app)
    assert client.get("/api/auth-config").json() == {"registration": "invite"}
    code, _ = _invite(app, "erin@example.com")

    missing = _register(client, "erin@example.com")
    wrong_email = _register(client, "mallory@example.com", invite_code=code)
    bogus = _register(client, "erin@example.com", invite_code="x" * 32)
    for response in (missing, wrong_email, bogus):
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "invalid_invite"
    assert count_argon2["hash"] == 0  # no hashing without a valid invite

    created = _register(client, "erin@example.com", invite_code=code)
    assert created.status_code == 201
    reused = _register(_client(app), "erin@example.com", invite_code=code)
    assert reused.status_code == 403
    with app.state.db.read() as conn:
        row = conn.execute("SELECT used_by, code_hash FROM invites").fetchone()
    assert row["used_by"] == created.json()["user"]["id"] and code not in row["code_hash"]


def test_invite_errors_do_not_reveal_registered_emails(tmp_path):
    app = _app(tmp_path, registration_mode="invite")
    code, _ = _invite(app, "frank@example.com")
    assert _register(_client(app), "frank@example.com", invite_code=code).status_code == 201
    registered = _register(_client(app), "frank@example.com")
    unregistered = _register(_client(app), "nobody@example.com")
    assert registered.status_code == unregistered.status_code == 403
    assert registered.json() == unregistered.json()


def test_expired_and_revoked_invites_fail_and_closed_mode_refuses(tmp_path):
    app = _app(tmp_path, registration_mode="invite")
    expired, _ = _invite(app, "gina@example.com", days=-1)
    revoked, revoked_id = _invite(app, "hank@example.com")
    with app.state.db.transaction() as conn:
        assert webdb.revoke_invite(conn, revoked_id)
    assert _register(_client(app), "gina@example.com", invite_code=expired).status_code == 403
    assert _register(_client(app), "hank@example.com", invite_code=revoked).status_code == 403

    closed = _register(_client(_app(tmp_path / "closed", registration_mode="closed")), "ivy@example.com")
    assert closed.status_code == 403 and closed.json()["detail"]["code"] == "registration_closed"
    with pytest.raises(WebConfigError):
        load_settings({"FINX_SECRET_KEY": SECRET, "FINX_REGISTRATION": "anyone"})
    assert load_settings({"FINX_SECRET_KEY": SECRET}).registration_mode == "invite"


def test_open_mode_hides_taken_emails_and_refuses_undeliverable_domains(tmp_path):
    client = _client(_app(tmp_path))
    assert _register(client, "jack@example.com").status_code == 201
    taken = _register(_client(client.app), "jack@example.com")
    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "registration_failed"
    assert "đã được sử dụng" not in taken.text
    for email in ("x@mail.invalid", "x@corp.test", "x@box.localhost"):
        assert _register(_client(client.app), email).json()["detail"]["code"] == "invalid_email"


def test_invites_cli_prints_the_code_once_and_stores_only_its_hash(tmp_path, monkeypatch, capsys):
    env = {"FINX_SECRET_KEY": SECRET, "FINX_WEB_DB": str(tmp_path / "web.sqlite3")}
    monkeypatch.setattr(webcli, "_prepare_environment", lambda *args: dict(env))
    assert webcli.invites_main(["list"]) == 2  # no database yet: never created implicitly
    assert webcli.db_main(["init"]) == 0
    capsys.readouterr()
    assert webcli.invites_main(["create", "--email", "Kim@Example.com"]) == 0
    created = capsys.readouterr().out
    code = created.split("code: ", 1)[1].split()[0]
    assert f"/signup?invite={code}" in created
    invite_id = created.split()[1]

    assert webcli.invites_main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "kim@example.com" in listed and "open" in listed and code not in listed
    assert webcli.invites_main(["create", "--email", "x@mail.invalid"]) == 2
    assert webcli.invites_main(["revoke", invite_id]) == 0
    assert webcli.invites_main(["revoke", invite_id]) == 1


# ------------------------------------------------------------- validation body


def test_validation_errors_never_echo_the_submitted_values(tmp_path):
    client = _client(_app(tmp_path))
    secret_password = "S3cret-" + "p" * 300
    for path, payload in (
        ("/api/register", {"name": "A", "email": "a@example.com", "password": secret_password}),
        ("/api/login", {"email": "a@example.com", "password": secret_password}),
    ):
        response = _post(client, path, payload)
        assert response.status_code == 422
        assert "S3cret-" not in response.text
        assert response.json()["detail"][0]["loc"] == ["body", "password"]
        assert set(response.json()["detail"][0]) == {"loc", "msg", "type"}
