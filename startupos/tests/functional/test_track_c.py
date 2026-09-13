"""Track C functional flow: onboarding tenant → connections → compile → confirm → cadence → status, end to end."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.conftest import login_as

pytestmark = pytest.mark.functional


@pytest.fixture()
def client(conn, auth_env):
    # Only touch the tenant this flow creates; other tracks share the scratch DB.
    for t in (
        "approvals",
        "tenant_jobs",
        "runs",
        "budgets",
        "brain_docs",
        "connections",
        "tenant_secrets",
        "sessions",
        "users",
        "issues",
        "projects",
        "accounts",
    ):
        conn.execute(f"DELETE FROM {t} WHERE tenant_id = 'acme-dev-tools'")
    conn.execute("DELETE FROM tenants WHERE id = 'acme-dev-tools'")
    conn.commit()
    from api.main import app

    with TestClient(app) as c:
        yield c
    # compile queues the first-pulse chain (Track O); leave no queued jobs behind for other tests' job service
    conn.execute("DELETE FROM tenant_jobs WHERE tenant_id = 'acme-dev-tools'")
    conn.commit()


def test_onboarding_flow(client, conn):
    # 1. tenant — sign-up needs the bootstrap token (or a Google id_token); the response sets the session cookie
    r = client.post(
        "/onboarding/tenant",
        json={"name": "Acme Dev Tools", "website": "acme.dev", "email": "founder@acme.dev"},
    )
    assert r.status_code == 401
    r = client.post(
        "/onboarding/tenant",
        json={
            "name": "Acme Dev Tools",
            "website": "acme.dev",
            "email": "founder@acme.dev",
            "bootstrap_token": "bootstrap-test-token",
        },
    )
    assert r.status_code == 200, r.text
    assert "sos_session" in r.cookies
    assert r.json()["user"]["email"] == "founder@acme.dev"
    body = r.json()
    tid = body["tenant"]["id"]
    assert tid == "acme-dev-tools"
    assert body["tenant"]["website"] == "https://acme.dev"
    assert body["seeded_slices"] == 5
    assert body["recommended_sources"] == ["linear", "slack"]  # 'dev' in the name/website
    assert len(body["sources"]) == 10
    owner = conn.execute("SELECT * FROM users WHERE tenant_id=%s", (tid,)).fetchone()
    assert owner["email"] == "founder@acme.dev" and owner["role"] == "owner"
    docs = conn.execute(
        "SELECT slice, source, content FROM brain_docs WHERE tenant_id=%s ORDER BY slice", (tid,)
    ).fetchall()
    assert [d["slice"] for d in docs] == ["icp", "identity", "pricing", "team", "voice"]
    assert all(d["source"] == "extracted" and "Draft — confirm or edit" in d["content"] for d in docs)
    assert any("https://acme.dev" in d["content"] for d in docs)

    q: dict[str, str] = {}  # tenant comes from the session cookie, never the query string
    st = client.get("/onboarding/status", params=q).json()
    assert st["steps"] == {
        "tenant": True,
        "connections": False,
        "compiled": False,
        "cards": False,
        "pulse": False,
        "cadence": False,
    }
    assert st["done"] == 1 and st["total"] == 6

    # 2. connections — secret_ref only, validated
    bad = client.post("/onboarding/connections", params=q, json={"source": "linear", "secret_ref": "lin_api_ABC123"})
    assert bad.status_code == 422
    assert (
        client.post("/onboarding/connections", params=q, json={"source": "fax", "secret_ref": "env:X"}).status_code
        == 422
    )
    # env: refs are the operator's (install tenant's) own keys — a self-serve tenant may not point at them
    r = client.post(
        "/onboarding/connections",
        params=q,
        json={"source": "linear", "secret_ref": "env:LINEAR_API_KEY", "config": {"team": "ACM"}},
    )
    assert r.status_code == 403, r.text
    assert conn.execute("SELECT count(*) AS n FROM connections WHERE tenant_id=%s", (tid,)).fetchone()["n"] == 0
    r = client.post(
        "/onboarding/connections",
        params=q,
        json={"source": "linear", "secret_ref": "kv:linear_api_key", "config": {"team": "ACM"}},
    )
    assert (
        r.status_code == 200
        and r.json()["secret_ref"] == "kv:linear_api_key"
        and r.json()["config"] == {"team": "ACM"}
        and r.json()["has_credential"] is False
    )
    assert client.get("/onboarding/status", params=q).json()["steps"]["connections"] is False
    r = client.post(
        "/onboarding/connections",
        params=q,
        json={"source": "slack", "secret_ref": "kv:acme-slack", "config": {"channels": ["C1"]}},
    )
    assert r.status_code == 200
    # upsert, not duplicate
    r = client.post("/onboarding/connections", params=q, json={"source": "slack", "secret_ref": "kv:acme-slack-2"})
    assert r.status_code == 200 and r.json()["secret_ref"] == "kv:acme-slack-2"
    assert conn.execute("SELECT count(*) AS n FROM connections WHERE tenant_id=%s", (tid,)).fetchone()["n"] == 2
    assert client.get("/onboarding/status", params=q).json()["steps"]["connections"] is True
    assert "lin_api" not in client.get("/onboarding/connections", params=q).text

    # 3. compile — deterministic from tables; seed a little ingested state first
    conn.execute(
        """INSERT INTO issues (tenant_id, id, title, status, status_type, priority, assignee, project, labels) VALUES
           (%s, 'ACM-1', 'A', 'Backlog', 'backlog', 2, 'Alex Dev', 'Alpha', '{}'),
           (%s, 'ACM-2', 'B', 'In Progress', 'started', 3, 'Alex Dev', 'Alpha', '{}'),
           (%s, 'ACM-3', 'C', 'Backlog', 'backlog', 3, 'Nanda', 'Beta', '{}')""",
        (tid, tid, tid),
    )
    conn.execute(
        "INSERT INTO accounts (tenant_id, id, name, stage, icp_score) VALUES (%s, 'jci', 'Johnson Controls', 'poc', 88), (%s, 'siemens', 'Siemens SI', 'target', 92)",
        (tid, tid),
    )
    conn.commit()
    r = client.post("/onboarding/compile", params=q)
    assert r.status_code == 200, r.text
    comp = r.json()
    assert comp["tier"] == 0
    assert comp["counts"]["issues"] == 3 and comp["counts"]["accounts"] == 2 and comp["counts"]["messages"] == 0
    assert comp["outcome"].startswith("Read 3 issues")
    cards = {c["slice"]: c for c in comp["cards"]}
    assert set(cards) == {"identity", "icp", "voice", "pricing", "team"}
    assert "Alex Dev — 2 issues" in cards["team"]["draft"] and "Nanda — 1 issues" in cards["team"]["draft"]
    assert (
        "issues.assignee:Alex Dev" in cards["team"]["sources"] and "users:founder@acme.dev" in cards["team"]["sources"]
    )
    assert "Siemens SI — target · ICP 92" in cards["icp"]["draft"]
    assert cards["icp"]["sources"] == ["accounts:Siemens SI", "accounts:Johnson Controls"]
    assert "brain_docs:pricing.md v1 (extracted)" in cards["pricing"]["sources"]
    assert all("Draft — confirm or edit" in c["draft"] for c in cards.values())
    st = client.get("/onboarding/status", params=q).json()
    assert st["steps"]["compiled"] is True and st["steps"]["cards"] is False
    run = conn.execute("SELECT * FROM runs WHERE tenant_id=%s", (tid,)).fetchone()
    assert (
        run["skill"] == "onboarding.compile"
        and run["tier"] == 0
        and run["model"] is None
        and float(run["cost_usd"]) == 0
    )

    # 4. confirm the five cards → brain_docs source=human, version bumps
    for s, c in cards.items():
        r = client.post(
            "/onboarding/confirm",
            params=q,
            json={"slice": s, "content": c["draft"].replace("Draft — confirm or edit.", "Confirmed.")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["source"] == "human" and r.json()["version"] == 2
    st = client.get("/onboarding/status", params=q).json()
    assert st["steps"]["cards"] is True and st["cards_confirmed"] == ["icp", "identity", "pricing", "team", "voice"]
    r = client.post("/onboarding/confirm", params=q, json={"slice": "team", "content": "Team v3"})
    assert r.json()["version"] == 3
    # recompiling now respects the human docs
    comp2 = client.post("/onboarding/compile", params=q).json()
    team2 = next(c for c in comp2["cards"] if c["slice"] == "team")
    assert team2["draft"] == "Team v3" and team2["sources"] == ["brain_docs:team.md v3 (human)"]

    # 5. first pulse — a daemon run; API only reads it
    assert st["steps"]["pulse"] is False
    conn.execute(
        "INSERT INTO runs (id, tenant_id, trigger, skill, tier, model, outcome) VALUES ('run_pulse', %s, 'schedule', 'cockpit.morning_pulse', 2, 'claude-sonnet-4-5', 'First pulse.')",
        (tid,),
    )
    conn.commit()
    ck = client.get("/cockpit", params=q).json()
    assert ck["pulse"]["text"] == "First pulse." and ck["pulse"]["source"] == "run"
    assert ck["tenant_id"] == tid and len(ck["tiles"]) == 4 and len(ck["quick_glance"]) == 6

    # 6. cadence
    r = client.post(
        "/onboarding/cadence",
        params=q,
        json={"timezone": "America/New_York", "pulse_hour": 7, "channel": "both", "tier2_tokens_allowed": 1500000},
    )
    assert r.status_code == 200
    assert conn.execute("SELECT timezone FROM tenants WHERE id=%s", (tid,)).fetchone()["timezone"] == "America/New_York"
    st = client.get("/onboarding/status", params=q).json()
    assert st["steps"] == {
        "tenant": True,
        "connections": True,
        "compiled": True,
        "cards": True,
        "pulse": True,
        "cadence": True,
    }
    assert st["done"] == 6

    assert client.get("/modules/build").json()["tiles"][0]["val"] == "3"
    # tenant scoping: `?tenant=` is ignored — a stranger's session sees none of this
    assert client.get("/onboarding/status", params={"tenant": tid}).json()["tenant_id"] == tid
    _, other_cookie = login_as(conn, "unitone", "someone@unitone.ai")
    client.cookies.set("sos_session", other_cookie)
    assert client.get("/onboarding/status", params={"tenant": tid}).json()["steps"]["compiled"] is False


def test_status_without_session_is_401(client):
    client.cookies.clear()
    r = client.get("/onboarding/status", params={"tenant": "ghost"})
    assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"
