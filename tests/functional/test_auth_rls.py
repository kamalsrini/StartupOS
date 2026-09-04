"""Tenant isolation THROUGH the API with the RLS role (STARTUPOS_API_DSN = startupos_app).

Cross-tenant ids 404; a second tenant created via /onboarding/tenant sees only its own rows.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import APP_TEST_DSN, login_as

pytestmark = pytest.mark.functional

BOOT = "bootstrap-test-token"
SECOND = "Auth Second Co"
SECOND_ID = "auth-second-co"


@pytest.fixture()
def client(conn, auth_env, monkeypatch):
    monkeypatch.setenv("STARTUPOS_API_DSN", APP_TEST_DSN)
    conn.execute("INSERT INTO tenants (id, name) VALUES ('other', 'Other Co') ON CONFLICT (id) DO NOTHING")
    for t in ("approvals", "sessions", "api_tokens", "brain_docs", "runs", "users"):
        conn.execute(f"DELETE FROM {t} WHERE tenant_id IN ('other', %s)", (SECOND_ID,))
    conn.execute("DELETE FROM approvals WHERE tenant_id = 'unitone' AND id LIKE 'rls-%'")
    conn.execute("DELETE FROM sessions WHERE tenant_id='unitone'")
    conn.execute("DELETE FROM tenants WHERE id = %s", (SECOND_ID,))
    conn.execute(
        """INSERT INTO approvals (id, tenant_id, module, type, target, preview, exec, status) VALUES
           ('rls-mine', 'unitone', 'build', 'Linear · assign', 'UNI-1 → A', 'mine', NULL, 'pending'),
           ('rls-theirs', 'other', 'build', 'Linear · assign', 'OTH-1 → B', 'theirs', NULL, 'pending')"""
    )
    conn.commit()
    from api.main import app

    with TestClient(app) as c:
        yield c
    # leave tenant 'unitone' as we found it — other tracks' tests count its approvals
    conn.execute("DELETE FROM approvals WHERE id LIKE 'rls-%'")
    conn.execute("DELETE FROM sessions WHERE tenant_id = 'unitone'")
    conn.commit()


def test_cross_tenant_approval_404_through_app_role(client, conn):
    user_id, cookie = login_as(conn, "unitone", "rls@unitone.ai")
    client.cookies.set("sos_session", cookie)
    assert client.get("/auth/me").json()["user"]["tenant_id"] == "unitone"
    assert client.get("/approvals/rls-mine").status_code == 200
    r = client.get("/approvals/rls-theirs")
    assert r.status_code == 404  # never 403: no existence leak
    assert client.post("/approvals/rls-theirs/decide", json={"decision": "approve"}).status_code == 404
    ids = {a["id"] for a in client.get("/approvals").json()}
    assert "rls-mine" in ids and "rls-theirs" not in ids
    # deciding our own row records our users.id even through the RLS role
    a = client.post("/approvals/rls-mine/decide", json={"decision": "decline", "reason": "no"}).json()
    assert a["decided_by"] == user_id
    assert conn.execute("SELECT status FROM approvals WHERE id='rls-theirs'").fetchone()["status"] == "pending"


def test_onboarding_tenant_creates_isolated_second_tenant(client, conn):
    assert client.post("/onboarding/tenant", json={"name": SECOND, "email": "founder@second.test"}).status_code == 401
    r = client.post(
        "/onboarding/tenant",
        json={"name": SECOND, "email": "founder@second.test", "bootstrap_token": "wrong"},
    )
    assert r.status_code == 403
    r = client.post(
        "/onboarding/tenant",
        json={"name": SECOND, "website": "second.test", "email": "founder@second.test", "bootstrap_token": BOOT},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tenant"]["id"] == SECOND_ID and body["user"]["tenant_id"] == SECOND_ID
    assert body["seeded_slices"] == 5 and "sos_session" in r.cookies

    me = client.get("/auth/me").json()
    assert me["user"]["tenant_id"] == SECOND_ID and me["user"]["email"] == "founder@second.test"
    # sees only its own data: no approvals, its own 5 brain docs, tiles at zero
    assert client.get("/approvals").json() == []
    assert client.get("/approvals/rls-mine").status_code == 404
    st = client.get("/onboarding/status").json()
    assert st["tenant_id"] == SECOND_ID and st["steps"]["tenant"] is True and st["done"] == 1
    assert client.get("/modules/build").json()["tiles"][0]["val"] == "0"
    # the superuser view confirms the rows landed under the new tenant only
    assert conn.execute("SELECT count(*) AS n FROM brain_docs WHERE tenant_id=%s", (SECOND_ID,)).fetchone()["n"] == 5
    assert conn.execute("SELECT count(*) AS n FROM users WHERE tenant_id=%s", (SECOND_ID,)).fetchone()["n"] == 1

    # a stranger cannot take over an existing company slug
    r = client.post("/onboarding/tenant", json={"name": SECOND, "email": "stranger@evil.test", "bootstrap_token": BOOT})
    assert r.status_code == 409

    # and the unitone user still sees none of it
    _, cookie = login_as(conn, "unitone", "rls2@unitone.ai")
    client.cookies.set("sos_session", cookie)
    assert client.get("/onboarding/status").json()["tenant_id"] == "unitone"
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM brain_docs WHERE tenant_id='unitone' AND path='identity.md' AND content LIKE %s",
            (f"%{SECOND}%",),
        ).fetchone()["n"]
        == 0
    )


def test_no_tenant_connection_sees_nothing(client):
    """A cookie forged with the right secret but unknown session id resolves to nothing."""
    from auth import sessions

    client.cookies.set("sos_session", sessions.sign_session_id("not-a-real-session"))
    assert client.get("/auth/me").status_code == 401
