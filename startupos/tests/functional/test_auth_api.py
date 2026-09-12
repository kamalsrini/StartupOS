"""Auth through the API (superuser API DSN): 401s, bootstrap → cookie, tokens, decided_by, logout, Google callback."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from common.settings import settings
from tests.conftest import login_as

pytestmark = pytest.mark.functional

T = settings.tenant_id  # bootstrap always targets TENANT_ID
BOOT = "bootstrap-test-token"


@pytest.fixture()
def client(conn, auth_env):
    from common.db import ensure_tenant

    ensure_tenant(conn, T, "UnitOne")
    for t in ("approvals", "runs", "sessions", "api_tokens"):
        conn.execute(f"DELETE FROM {t} WHERE tenant_id = %s", (T,))
    conn.execute("DELETE FROM users WHERE tenant_id = %s AND email LIKE '%%@auth.test'", (T,))
    conn.commit()
    from api.main import app

    with TestClient(app) as c:
        yield c
    conn.execute("DELETE FROM approvals WHERE id = 'auth-a1'")
    conn.execute("DELETE FROM runs WHERE tenant_id = %s AND skill = 'onboarding.compile'", (T,))
    conn.commit()


def test_unauthenticated_is_401_with_bearer_challenge(client):
    for path in ("/modules/build", "/approvals", "/cockpit", "/finance", "/auth/me", "/onboarding/status"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert r.headers.get("www-authenticate") == "Bearer"
    assert client.get("/modules/build", params={"tenant": T}).status_code == 401  # ?tenant= is gone
    assert client.get("/modules/build", headers={"X-Tenant-Id": T}).status_code == 401
    assert client.get("/health").status_code == 200 and client.get("/health").json()["tenant"] is None


def test_bootstrap_then_cookie_then_modules(client, conn):
    assert client.post("/auth/bootstrap", json={"token": "wrong", "email": "kamal@auth.test"}).status_code == 403
    r = client.post("/auth/bootstrap", json={"token": BOOT, "email": "Kamal@auth.test", "name": "Kamal"})
    assert r.status_code == 200, r.text
    assert r.json()["via"] == "bootstrap" and r.json()["user"]["email"] == "kamal@auth.test"
    cookie = r.cookies.get("sos_session")
    assert (
        cookie
        and "HttpOnly" in r.headers["set-cookie"]
        and "SameSite=lax" in r.headers["set-cookie"].replace("Lax", "lax")
    )
    assert "Secure" not in r.headers["set-cookie"]  # testserver counts as localhost

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["via"] == "session" and me.json()["user"]["tenant_id"] == T
    assert me.json()["user"]["id"] == f"{T}:kamal@auth.test" and me.json()["user"]["role"] == "owner"
    assert client.get("/modules/build").status_code == 200
    assert client.get("/health").json()["tenant"] == T
    row = conn.execute("SELECT status, role FROM users WHERE id = %s", (f"{T}:kamal@auth.test",)).fetchone()
    assert row["status"] == "active" and row["role"] == "owner"


def test_bootstrap_disabled_is_404(client, monkeypatch):
    monkeypatch.setenv("STARTUPOS_BOOTSTRAP_TOKEN", "")
    assert client.post("/auth/bootstrap", json={"token": BOOT, "email": "x@auth.test"}).status_code == 404


def test_api_token_lifecycle(client, conn):
    user_id, cookie = login_as(conn, T, "tok@auth.test")
    client.cookies.set("sos_session", cookie)
    r = client.post("/auth/tokens", json={"name": "cli"})
    assert r.status_code == 200, r.text
    tid, plaintext = r.json()["id"], r.json()["token"]
    assert plaintext.startswith("sos_") and tid.startswith("tok_")
    stored = conn.execute("SELECT * FROM api_tokens WHERE id = %s", (tid,)).fetchone()
    assert stored["token_hash"] != plaintext and plaintext not in json.dumps(dict(stored), default=str)

    listed = client.get("/auth/tokens").json()
    assert [t["id"] for t in listed] == [tid] and "token_hash" not in listed[0] and "token" not in listed[0]

    client.cookies.clear()
    bearer = {"Authorization": f"Bearer {plaintext}"}
    me = client.get("/auth/me", headers=bearer)
    assert me.status_code == 200 and me.json()["via"] == "token" and me.json()["user"]["id"] == user_id
    assert client.get("/modules/build", headers=bearer).status_code == 200
    assert conn.execute("SELECT last_used_at FROM api_tokens WHERE id = %s", (tid,)).fetchone()["last_used_at"]
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {plaintext[:-1]}x"}).status_code == 401

    assert client.delete(f"/auth/tokens/{tid}", headers=bearer).status_code == 200
    assert client.get("/auth/me", headers=bearer).status_code == 401
    assert client.get("/modules/build", headers=bearer).status_code == 401


def test_bearer_beats_cookie(client, conn):
    u1, cookie = login_as(conn, T, "cookie@auth.test")
    u2, cookie2 = login_as(conn, T, "bearer@auth.test")
    client.cookies.set("sos_session", cookie2)
    plaintext = client.post("/auth/tokens", json={"name": "x"}).json()["token"]
    client.cookies.set("sos_session", cookie)
    assert client.get("/auth/me").json()["user"]["id"] == u1
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {plaintext}"}).json()
    assert me["user"]["id"] == u2 and me["via"] == "token"


def test_decide_records_authenticated_user(client, conn):
    user_id, cookie = login_as(conn, T, "decider@auth.test")
    client.cookies.set("sos_session", cookie)
    conn.execute(
        """INSERT INTO approvals (id, tenant_id, module, type, target, preview, exec, status)
           VALUES ('auth-a1', %s, 'build', 'Linear · assign', 'UNI-1 → A', 'Assign', NULL, 'pending')""",
        (T,),
    )
    conn.commit()
    r = client.post("/approvals/auth-a1/decide", json={"decision": "approve", "decided_by": "spoofed"})
    assert r.status_code == 200, r.text
    assert r.json()["decided_by"] == user_id
    assert conn.execute("SELECT decided_by FROM approvals WHERE id='auth-a1'").fetchone()["decided_by"] == user_id


def test_compile_records_acted_by(client, conn):
    user_id, cookie = login_as(conn, T, "compiler@auth.test")
    client.cookies.set("sos_session", cookie)
    assert client.post("/onboarding/compile").status_code == 200
    row = conn.execute(
        "SELECT acted_by FROM runs WHERE tenant_id=%s AND skill='onboarding.compile' ORDER BY started_at DESC LIMIT 1",
        (T,),
    ).fetchone()
    assert row["acted_by"] == user_id


def test_logout_revokes_session(client, conn):
    _, cookie = login_as(conn, T, "bye@auth.test")
    client.cookies.set("sos_session", cookie)
    assert client.get("/auth/me").status_code == 200
    r = client.post("/auth/logout")
    assert r.status_code == 200 and r.json() == {"ok": True}
    client.cookies.set("sos_session", cookie)  # replaying the old cookie must fail: server-side revoke
    assert client.get("/auth/me").status_code == 401
    assert client.get("/modules/build").status_code == 401
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM sessions WHERE tenant_id=%s AND revoked_at IS NOT NULL", (T,)
        ).fetchone()["n"]
        >= 1
    )


def test_disabled_user_is_rejected(client, conn):
    user_id, cookie = login_as(conn, T, "gone@auth.test")
    client.cookies.set("sos_session", cookie)
    assert client.get("/auth/me").status_code == 200
    conn.execute("UPDATE users SET status='disabled' WHERE id=%s", (user_id,))
    conn.commit()
    assert client.get("/auth/me").status_code == 401


def test_slack_link_maps_user(client, conn):
    user_id, cookie = login_as(conn, T, "slacker@auth.test")
    client.cookies.set("sos_session", cookie)
    assert client.post("/auth/slack/link", json={"slack_user_id": "u123"}).status_code == 422
    r = client.post("/auth/slack/link", json={"slack_user_id": "U0AUTHTEST"})
    assert r.status_code == 200 and r.json()["user_id"] == user_id
    from daemon.gateway.slack import resolve_slack_user
    from tests.conftest import APP_TEST_DSN

    assert resolve_slack_user("U0AUTHTEST", APP_TEST_DSN) == (T, user_id)
    assert resolve_slack_user("UNKNOWN", APP_TEST_DSN) is None


# --- Google callback (network stubbed) ---------------------------------------------


@pytest.fixture()
def google_stub(monkeypatch):
    from auth import google

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("STARTUPOS_WEB_URL", "http://web.test:3000")
    who = {"email": "kamal@auth.test", "sub": "g-sub-1", "name": "Kamal G"}
    monkeypatch.setattr(
        google,
        "discovery",
        lambda client=None: {"authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth"},
    )
    monkeypatch.setattr(google, "exchange_code", lambda code, client=None: {"id_token": "stub"})

    def verify(id_token, nonce=None, client=None, *, audience=None):
        assert id_token == "stub" and nonce is not None
        return {**who, "email_verified": True, "iss": "https://accounts.google.com"}

    monkeypatch.setattr(google, "verify_id_token", verify)
    return who


def _start(client):
    r = client.get("/auth/google", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://accounts.google.com/")
    assert "sos_oauth" in r.cookies
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["scope"] == ["openid email profile"] and q["redirect_uri"][0].endswith("/auth/google/callback")
    return q["state"][0]


def test_google_not_configured_503(client):
    assert client.get("/auth/google", follow_redirects=False).status_code == 503


def test_google_unknown_email_403(client, google_stub):
    google_stub["email"] = "stranger@auth.test"
    state = _start(client)
    r = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.status_code == 403
    assert r.json()["detail"] == "No account for this email — ask your company owner to invite you"
    assert "sos_session" not in r.cookies


def test_google_state_mismatch_403(client, google_stub, conn):
    login_as(conn, T, "kamal@auth.test")
    _start(client)
    r = client.get("/auth/google/callback", params={"code": "c", "state": "forged"}, follow_redirects=False)
    assert r.status_code == 403


def test_google_known_email_sets_cookie_and_redirects(client, google_stub, conn):
    user_id, _ = login_as(conn, T, "kamal@auth.test")
    state = _start(client)
    r = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.status_code == 302, r.text
    assert r.headers["location"] == "http://web.test:3000/cockpit"
    assert "sos_session" in r.cookies
    me = client.get("/auth/me")
    assert me.status_code == 200 and me.json()["user"]["id"] == user_id and me.json()["via"] == "session"
    row = conn.execute("SELECT google_sub, last_login_at FROM users WHERE id=%s", (user_id,)).fetchone()
    assert row["google_sub"] == "g-sub-1" and row["last_login_at"] is not None
    # second sign-in resolves by sub even if the email casing differs
    google_stub["email"] = "KAMAL@auth.test"
    state = _start(client)
    assert (
        client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False).status_code
        == 302
    )


def test_startup_refuses_without_secret(monkeypatch):
    monkeypatch.delenv("STARTUPOS_SESSION_SECRET", raising=False)
    monkeypatch.delenv("STARTUPOS_DEV", raising=False)
    from api.main import check_startup_config

    with pytest.raises(RuntimeError, match="STARTUPOS_SESSION_SECRET"):
        check_startup_config()
    monkeypatch.setenv("STARTUPOS_DEV", "1")
    check_startup_config()
