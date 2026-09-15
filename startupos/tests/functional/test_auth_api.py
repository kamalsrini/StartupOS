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
    # One character different, same length. Substituting a FIXED character was a 1-in-64 no-op: tokens end in a
    # urlsafe-base64 character, so roughly one run in sixty-four mutated the token into itself and asserted that
    # a VALID token is rejected. Flip to a character the last one is not.
    tampered = plaintext[:-1] + ("y" if plaintext[-1] == "x" else "x")
    assert tampered != plaintext
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {tampered}"}).status_code == 401

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
    assert r.status_code == 200 and r.json() == {"ok": True, "revoked": True}
    client.cookies.set("sos_session", cookie)  # replaying the old cookie must fail: server-side revoke
    assert client.get("/auth/me").status_code == 401
    assert client.get("/modules/build").status_code == 401
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM sessions WHERE tenant_id=%s AND revoked_at IS NOT NULL", (T,)
        ).fetchone()["n"]
        >= 1
    )
    # Signing out when the session is already gone still clears the cookie (Sprint 3d, Track G) — it used to
    # 401, which left the dead cookie in the browser on the one page where "Sign out" is what you press.
    client.cookies.clear()
    r = client.post("/auth/logout")
    assert r.status_code == 200 and r.json() == {"ok": True, "revoked": False}
    assert "sos_session=" in r.headers.get("set-cookie", "")


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
        assert id_token == "stub"  # nonce is None on the sign-up path (POST /onboarding/tenant), set on callback
        if who.get("email_verified") is False:
            raise google.UnverifiedEmailError("invalid id_token: email not verified")
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


def test_google_new_identity_is_offered_a_company_of_its_own(client, google_stub, conn):
    """Sprint 3d (Track G): a verified identity nobody has ever seen is a sign-up, not a dead end.

    It must be a NEW tenant — sharing the URL only works if a stranger can get in — and it must never be a way
    into somebody else's company.
    """
    google_stub["email"] = "stranger@auth.test"
    google_stub["sub"] = "g-sub-stranger"
    state = _start(client)
    r = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.status_code == 302, r.text
    # The sign-in page, told who it is talking to, so it can say "this creates a NEW company".
    assert r.headers["location"] == "http://web.test:3000/login?new=google&email=stranger%40auth.test"
    assert "sos_signup" in r.cookies and "sos_session" not in r.cookies  # verified, but not signed in yet
    assert client.get("/auth/me").status_code == 401

    # ... and the sign-up cookie is what POST /onboarding/tenant accepts (no id_token ever reaches JavaScript).
    from auth import google as g

    assert g.read_signup_cookie(r.cookies["sos_signup"]) == "stub"
    try:
        made = client.post("/onboarding/tenant", json={"name": "Stranger Co", "website": "stranger.test"})
        assert made.status_code == 200, made.text
        new_tenant = made.json()["tenant"]["id"]
        assert new_tenant == "stranger-co" and new_tenant != T
        assert made.json()["owner_email"] == "stranger@auth.test"
        me = client.get("/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["tenant_id"] == new_tenant and me.json()["user"]["role"] == "owner"
        assert client.cookies.get("sos_signup") in (None, "")  # spent, and cleared
        # The stranger's brand-new company shares nothing with the install's own tenant.
        assert conn.execute("SELECT count(*) AS n FROM users WHERE tenant_id = %s", (new_tenant,)).fetchone()["n"] == 1
    finally:
        for table in ("sessions", "brain_docs", "users"):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = 'stranger-co'")
        conn.execute("DELETE FROM tenants WHERE id = 'stranger-co'")
        conn.commit()


def test_google_signup_cookie_is_required_and_verified(client, google_stub, conn):
    """No cookie (or a forged one) is not a sign-up: POST /onboarding/tenant stays shut."""
    r = client.post("/onboarding/tenant", json={"name": "No Cookie Co"})
    assert r.status_code == 401
    client.cookies.set("sos_signup", "forged.cookie.value")
    assert client.post("/onboarding/tenant", json={"name": "No Cookie Co"}).status_code == 401
    client.cookies.clear()
    assert conn.execute("SELECT count(*) AS n FROM tenants WHERE id = 'no-cookie-co'").fetchone()["n"] == 0


def test_google_unverified_email_is_refused(client, google_stub, conn):
    """Google authenticated the account but will not vouch for the address — no session, and no sign-up either."""
    google_stub["email"] = "unverified@auth.test"
    google_stub["email_verified"] = False
    state = _start(client)
    r = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.status_code == 403
    assert "sos_session" not in r.cookies and "sos_signup" not in r.cookies
    assert "not verified" in r.text  # a page a person can read, not a bare JSON body on an API URL
    r = client.get(
        "/auth/google/callback",
        params={"code": "c", "state": state},
        headers={"accept": "application/json"},
        follow_redirects=False,
    )
    assert r.status_code == 403 and r.json()["reason"] == "email_unverified"
    assert conn.execute("SELECT count(*) AS n FROM users WHERE email = 'unverified@auth.test'").fetchone()["n"] == 0


def test_google_inactive_account_is_refused_not_given_a_second_company(client, google_stub, conn):
    """An address that already belongs to a company never gets a brand-new empty one (invite-only stays)."""
    user_id, _ = login_as(conn, T, "disabled@auth.test")
    conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (user_id,))
    conn.commit()
    google_stub["email"] = "disabled@auth.test"
    google_stub["sub"] = "g-sub-disabled"
    state = _start(client)
    r = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.status_code == 403
    assert "sos_session" not in r.cookies and "sos_signup" not in r.cookies
    assert "not active" in r.text


def test_google_callback_hit_directly_is_refused(client, google_stub):
    """No cookie at all: a bookmarked callback, a 10-minute-old tab, or someone poking at the URL."""
    r = client.get("/auth/google/callback", params={"code": "c", "state": "whatever"}, follow_redirects=False)
    assert r.status_code == 403 and "sos_session" not in r.cookies
    assert client.get("/auth/google/callback", follow_redirects=False).status_code == 403
    r = client.get("/auth/google/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert r.status_code == 403  # the user pressed "cancel" at Google


def test_google_stale_oauth_cookie_is_refused(client, google_stub, monkeypatch):
    """The state cookie is good for one attempt and ten minutes; after that, start again."""
    from auth import google as g

    state = _start(client)
    monkeypatch.setattr(g, "OAUTH_COOKIE_MAX_AGE", -1)
    r = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert r.status_code == 403 and "expired" in r.text


def test_google_signs_in_to_the_users_own_tenant_not_the_installs(client, google_stub, conn):
    """The email belongs to a user in ANOTHER company: they land in that company, never in TENANT_ID."""
    from common.db import ensure_tenant

    other = "othercorp"
    ensure_tenant(conn, other, "Other Corp")
    conn.commit()
    other_user, _ = login_as(conn, other, "elsewhere@auth.test")
    login_as(conn, T, "elsewhere-decoy@auth.test")  # a user with a different address in the install's tenant
    google_stub["email"] = "elsewhere@auth.test"
    google_stub["sub"] = "g-sub-elsewhere"
    try:
        state = _start(client)
        r = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "http://web.test:3000/cockpit"
        me = client.get("/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["id"] == other_user and me.json()["user"]["tenant_id"] == other
        assert client.get("/health").json()["tenant"] == other
    finally:
        client.cookies.clear()
        for table in ("sessions", "users"):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (other,))
        conn.execute("DELETE FROM tenants WHERE id = %s", (other,))
        conn.commit()


def test_auth_providers_is_public_and_boolean_only(client, google_stub):
    r = client.get("/auth/providers")
    assert r.status_code == 200 and r.json() == {"google": True, "bootstrap": True, "signup": True}
    body = r.text
    assert "cid.apps.googleusercontent.com" not in body and "csecret" not in body


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


# --- PE review, Sprint 3d: what a stranger on the public internet can do to sign-up ----------------
#
# The deployment moved from a VM with every port closed to one public hostname with self-serve Google sign-up.
# These three are the holes that change from "theoretical" to "reachable by anyone with the link".


def test_signup_is_rate_limited_like_the_bootstrap_route_it_shares_a_secret_with(client, conn):
    """POST /onboarding/tenant checks the SAME bootstrap token as POST /auth/bootstrap, which is throttled.

    Unthrottled, the brute force simply moves next door — and every company this route creates arrives with its
    own monthly Tier-2 token budget the daemon will spend, so it is also the cheapest way to cost the operator
    real money.
    """
    from api import ratelimit

    ratelimit.reset_all()
    seen = [
        client.post(
            "/onboarding/tenant", json={"name": f"Guessing Co {i}", "bootstrap_token": f"guess-{i}"}
        ).status_code
        for i in range(7)
    ]
    assert seen[:5] == [403] * 5, seen  # a wrong token is a 403 …
    assert seen[5:] == [429, 429], seen  # … and then the door closes for a minute
    assert conn.execute("SELECT count(*) AS n FROM tenants WHERE id LIKE 'guessing-co%'").fetchone()["n"] == 0

    # The limit is per source, and the source is the client the PROXY saw — not a header the caller picked.
    ratelimit.reset_all()
    for i in range(5):
        client.post(
            "/onboarding/tenant",
            json={"name": f"Spoof Co {i}", "bootstrap_token": "guess"},
            headers={"x-forwarded-for": f"10.0.0.{i}, 203.0.113.9"},
        )
    blocked = client.post(
        "/onboarding/tenant",
        json={"name": "Spoof Co 9"},
        headers={"x-forwarded-for": "10.0.0.99, 203.0.113.9"},
    )
    assert blocked.status_code == 429, blocked.text
    ratelimit.reset_all()


def test_a_google_identity_that_already_has_a_company_cannot_mint_a_second_one(client, google_stub, conn):
    """The callback only ever issues `sos_signup` for an identity it found no user for — but `google_id_token`
    is a documented field on POST /onboarding/tenant too, and that path skipped the check entirely.

    CONTRACTS.md Sprint 3d states the rule as an invariant of the product ("an address that already belongs to a
    company never gets a second, empty one"), so it has to hold on every way in, not just the browser one.
    """
    login_as(conn, T, "settled@auth.test")
    google_stub["email"] = "settled@auth.test"
    google_stub["sub"] = "g-sub-settled"
    r = client.post("/onboarding/tenant", json={"name": "Second Co", "google_id_token": "stub"})
    assert r.status_code == 403, r.text
    assert "already has a StartupOS account" in r.json()["detail"]
    assert conn.execute("SELECT count(*) AS n FROM tenants WHERE id = 'second-co'").fetchone()["n"] == 0
    assert client.get("/auth/me").status_code == 401  # and no session was handed out either

    # Re-running sign-up for the company you DO belong to is still fine: that is the wizard going back a step.
    from common.db import ensure_tenant

    ensure_tenant(conn, "reentry-co", "Reentry Co")
    login_as(conn, "reentry-co", "returning@auth.test")
    conn.commit()
    google_stub["email"] = "returning@auth.test"
    google_stub["sub"] = "g-sub-returning"
    try:
        again = client.post("/onboarding/tenant", json={"name": "Reentry Co", "google_id_token": "stub"})
        assert again.status_code == 200, again.text
        assert again.json()["tenant"]["id"] == "reentry-co"
    finally:
        for table in ("sessions", "brain_docs", "users"):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = 'reentry-co'")
        conn.execute("DELETE FROM tenants WHERE id = 'reentry-co'")
        conn.commit()


def test_a_stranger_cannot_rename_an_existing_company_by_signing_up_with_its_name(client, google_stub, conn):
    """Sign-up UPSERTs `tenants` before it discovers the company already has members and answers 409.

    The 409 has to leave that company exactly as it was — name, website and all — which it does only because
    `common.db.get_conn` rolls the transaction back when the handler raises. That is a load-bearing property of
    a helper three modules away, so it gets a test of its own here rather than a comment.
    """
    conn.execute("UPDATE tenants SET name = 'UnitOne', website = 'https://unitone.ai' WHERE id = %s", (T,))
    login_as(conn, T, "owner@auth.test")
    conn.commit()
    google_stub["email"] = "outsider@auth.test"
    google_stub["sub"] = "g-sub-outsider"
    r = client.post(
        "/onboarding/tenant",
        json={"name": T, "website": "https://attacker.test", "google_id_token": "stub"},
    )
    assert r.status_code == 409, r.text
    row = conn.execute("SELECT name, website FROM tenants WHERE id = %s", (T,)).fetchone()
    assert row["name"] == "UnitOne" and row["website"] == "https://unitone.ai"


# --- what a self-serve company costs, and the operator's off switch (Sprint 3d PE review, fix 1) ------------


def _delete_tenant(conn, tenant_id):
    for table in ("sessions", "brain_docs", "budgets", "users"):
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))  # noqa: S608 - literal names
    conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
    conn.commit()


def test_a_self_serve_company_arrives_on_the_small_tier_and_an_operator_created_one_does_not(
    client, google_stub, conn, monkeypatch
):
    """Sharing the URL means strangers create companies the daemon spends real money for. Sign-up puts them on
    `self_serve`, whose monthly Tier-2 allowance is STARTUPOS_SIGNUP_TIER2_TOKENS — a fraction of the founder
    tier an operator's own install keeps."""
    from daemon import budget

    monkeypatch.setenv("STARTUPOS_SIGNUP_TIER2_TOKENS", "40000")
    google_stub["email"] = "capped@auth.test"
    google_stub["sub"] = "g-sub-capped"
    try:
        made = client.post("/onboarding/tenant", json={"name": "Capped Co", "google_id_token": "stub"})
        assert made.status_code == 200, made.text
        assert made.json()["tenant"]["tier"] == budget.SELF_SERVE_TIER
        # The budget follows from the tier, every month, with nothing to remember to set.
        assert budget.allowed_for(conn, "capped-co") == 40_000
        assert budget.get_or_create(conn, "capped-co")["tier2_tokens_allowed"] == 40_000
        conn.commit()

        # …and the tenant cannot lift its own cap through the cadence form it owns.
        over = client.post("/onboarding/cadence", json={"tier2_tokens_allowed": 1_500_000})
        assert over.status_code == 422, over.text
        assert "40,000" in over.json()["detail"]
        ok = client.post("/onboarding/cadence", json={"tier2_tokens_allowed": 10_000})
        assert ok.status_code == 200 and ok.json()["tier2_tokens_allowed"] == 10_000
        assert ok.json()["tier2_tokens_ceiling"] == 40_000
        assert client.get("/onboarding/status").json()["tier2_tokens_ceiling"] == 40_000
        conn.commit()

        # An operator promoting a real customer is one UPDATE, and the allowance follows.
        conn.execute("UPDATE tenants SET tier = 'founder' WHERE id = 'capped-co'")
        conn.commit()
        assert budget.allowed_for(conn, "capped-co") == 1_500_000
    finally:
        client.cookies.clear()
        _delete_tenant(conn, "capped-co")


def test_an_operator_created_company_keeps_the_founder_budget(client, conn):
    """The bootstrap-token path is the operator making a company on purpose; nothing about it changes."""
    from daemon import budget

    try:
        made = client.post(
            "/onboarding/tenant",
            json={"name": "Operator Co", "bootstrap_token": BOOT, "email": "operator@auth.test"},
        )
        assert made.status_code == 200, made.text
        assert made.json()["tenant"]["tier"] == "founder"
        assert budget.allowed_for(conn, "operator-co") == budget.ALLOWED_BY_TIER["founder"] == 1_500_000
        cadence = client.post("/onboarding/cadence", json={"tier2_tokens_allowed": 1_500_000})
        assert cadence.status_code == 200 and cadence.json()["tier2_tokens_allowed"] == 1_500_000
        conn.commit()
    finally:
        client.cookies.clear()
        _delete_tenant(conn, "operator-co")


def test_with_signup_closed_the_route_refuses_and_the_sign_in_page_is_told(client, google_stub, conn, monkeypatch):
    """STARTUPOS_ALLOW_SIGNUP=0 is the operator's off switch: no new companies, no dead ends, and sign-in for
    people who already have one is untouched."""
    monkeypatch.setenv("STARTUPOS_ALLOW_SIGNUP", "0")
    google_stub["email"] = "shutout@auth.test"
    google_stub["sub"] = "g-sub-shutout"

    # 1. the sign-in page reads this and says so instead of offering a sign-up that cannot work.
    assert client.get("/auth/providers").json() == {"google": True, "bootstrap": True, "signup": False}

    # 2. the unauthenticated write refuses, with copy a person can act on, and creates nothing.
    r = client.post("/onboarding/tenant", json={"name": "Shutout Co", "google_id_token": "stub"})
    assert r.status_code == 403, r.text
    assert "not accepting new companies" in r.json()["detail"] and "invited" in r.json()["detail"]
    assert conn.execute("SELECT count(*) AS n FROM tenants WHERE id = 'shutout-co'").fetchone()["n"] == 0
    assert client.get("/auth/me").status_code == 401

    # 3. the Google round trip says it on the page, rather than handing out a sign-up cookie that will be refused.
    state = _start(client)
    cb = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert cb.status_code == 403
    assert "sos_signup" not in cb.cookies and "sos_session" not in cb.cookies
    assert "not accepting new companies" in cb.text
    assert "signup_closed" in cb.headers.get("location", "") or "signup_closed" in cb.text

    # 4. somebody who already has an account still signs in normally.
    login_as(conn, T, "member@auth.test")
    conn.commit()
    google_stub["email"] = "member@auth.test"
    google_stub["sub"] = "g-sub-member"
    client.cookies.clear()
    state = _start(client)
    cb = client.get("/auth/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    assert cb.status_code == 302 and cb.headers["location"].endswith("/cockpit")
    assert "sos_session" in cb.cookies
    client.cookies.clear()


def test_signup_closed_does_not_close_the_operators_own_bootstrap(client, conn, monkeypatch):
    """Closing self-serve sign-up must not lock the operator out of making a company deliberately."""
    monkeypatch.setenv("STARTUPOS_ALLOW_SIGNUP", "0")
    try:
        made = client.post(
            "/onboarding/tenant",
            json={"name": "Still Allowed Co", "bootstrap_token": BOOT, "email": "operator2@auth.test"},
        )
        assert made.status_code == 200, made.text
        assert made.json()["tenant"]["id"] == "still-allowed-co"
        conn.commit()
    finally:
        client.cookies.clear()
        _delete_tenant(conn, "still-allowed-co")
