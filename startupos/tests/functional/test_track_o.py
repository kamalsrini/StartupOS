"""Track O — onboarding pipeline against Postgres (CONTRACTS.md "Track O", Architecture Brief §5.1 / §7 Week 4):

Acceptance: tenant B is created through the API, connects Linear with the credential "fixture", calls
POST /onboarding/compile, and the daemon's job service (as the RLS-bound `startupos_app` role, no ANTHROPIC key, no
operator env) drives the chain until idle. B then has issues, signals, a context pack and a `cockpit.morning_pulse`
run; every other tenant's counts are unchanged. Plus the queue rules: per-tenant ordering, idempotent enqueue,
3 attempts with backoff, the chain continuing past a failed backfill, and RLS on `tenant_jobs`.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from common import jobs as queue
from common.tenants import tenant_conn
from daemon import jobs, llm
from tests.conftest import APP_TEST_DSN

pytestmark = pytest.mark.functional

TB = "beta-robotics"  # slugify("Beta Robotics")
TA = "unitone"  # the pre-existing tenant whose counts must not move
COUNTED = ("issues", "projects", "signals", "context_packs", "runs", "events", "approvals", "tenant_jobs")
PULSE = "cockpit.morning_pulse"


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def counts_except(conn, tenant_id: str) -> dict[str, dict[str, int]]:
    """Row counts per table per tenant, for every tenant except `tenant_id`."""
    out: dict[str, dict[str, int]] = {}
    for t in COUNTED:
        for r in conn.execute(
            f"SELECT tenant_id, count(*) AS n FROM {t} WHERE tenant_id <> %s GROUP BY tenant_id", (tenant_id,)
        ).fetchall():
            out.setdefault(r["tenant_id"], {})[t] = int(r["n"])
    return out


def wipe_tenant(conn, tenant_id: str) -> None:
    for t in (
        "approvals",
        "signals",
        "tenant_jobs",
        "runs",
        "context_packs",
        "events",
        "issues",
        "projects",
        "budgets",
        "brain_docs",
        "connections",
        "tenant_secrets",
        "sessions",
        "users",
    ):
        conn.execute(f"DELETE FROM {t} WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
    conn.commit()


@pytest.fixture()
def no_model(monkeypatch):
    """No ANTHROPIC key, no operator source keys, and the real client factory (so LLMUnavailable → Tier 0)."""
    for var in ("ANTHROPIC_API_KEY", "LINEAR_API_KEY", "SLACK_BOT_TOKEN", "BREX_API_TOKEN", "VERCEL_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(llm, "_client_factory", llm._default_client_factory)
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", _key())
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0))  # retries are immediate in tests


@pytest.fixture()
def client(conn, auth_env, no_model):
    wipe_tenant(conn, TB)
    # Other tracks' tests call POST /onboarding/compile too and leave their chains queued; the drain below is
    # cross-tenant by design, so start from an idle queue to assert exactly what B's chain did.
    conn.execute("DELETE FROM tenant_jobs WHERE status = ANY(%s)", (list(queue.ACTIVE),))
    conn.commit()
    from api.main import app

    with TestClient(app) as c:
        yield c
    wipe_tenant(conn, TB)


# --- acceptance ---------------------------------------------------------------------------------------------


def test_second_tenant_gets_a_pulse_through_the_api_alone(client, conn):
    # tenant A exists with something in it; its numbers are the control
    conn.execute(
        "INSERT INTO issues (tenant_id, id, title, status, status_type, priority) VALUES (%s, 'UNI-O-1', 'control', 'Backlog', 'backlog', 2) "
        "ON CONFLICT (tenant_id, id) DO NOTHING",
        (TA,),
    )
    conn.commit()
    before = counts_except(conn, TB)

    # 1. sign-up through the API (bootstrap token — the only unauthenticated write)
    r = client.post(
        "/onboarding/tenant",
        json={
            "name": "Beta Robotics",
            "website": "beta.dev",
            "email": "cto@beta.dev",
            "bootstrap_token": "bootstrap-test-token",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant"]["id"] == TB

    # 2. its own Linear key — the literal "fixture" makes ingest read tests/fixtures; nothing touches .env
    r = client.post("/onboarding/connections", json={"source": "linear", "credential": "fixture"})
    assert r.status_code == 200, r.text
    assert r.json()["secret_ref"] == "kv:linear_api_key" and r.json()["has_credential"] is True
    assert "fixture" not in client.get("/onboarding/connections").text

    # 3. compile returns immediately with cards + the queued chain
    r = client.post("/onboarding/compile")
    assert r.status_code == 200, r.text
    comp = r.json()
    assert {c["slice"] for c in comp["cards"]} == {"identity", "icp", "voice", "pricing", "team"}
    assert comp["counts"]["issues"] == 0  # nothing ingested yet — the daemon does that
    assert [j["kind"] for j in comp["jobs"]] == [
        "backfill:linear",
        "signals",
        "context_pack",
        "chief_of_staff",
        "morning_pulse",
    ]
    assert all(j["status"] == "queued" and j["enqueued"] for j in comp["jobs"])
    st = client.get("/onboarding/status").json()
    assert st["jobs"]["queued"] == 5 and st["jobs"]["running"] == 0 and st["first_pulse_ready"] is False
    assert [j["kind"] for j in st["jobs"]["chain"]] == [j["kind"] for j in comp["jobs"]]
    # idempotent: compiling again while the chain is queued adds nothing
    again = client.post("/onboarding/compile").json()
    assert not any(j["enqueued"] for j in again["jobs"]) and [j["id"] for j in again["jobs"]] == [
        j["id"] for j in comp["jobs"]
    ]
    assert client.get("/onboarding/status").json()["jobs"]["queued"] == 5

    # 4. the daemon's job service, as startupos_app, drives the chain until idle — no model, no env keys
    out = jobs.drain(dsn=APP_TEST_DSN)
    assert out["claimed"] == 5 and out["done"] == 5 and out["failed"] == 0, out
    assert [j["kind"] for j in out["jobs"]] == [j["kind"] for j in comp["jobs"]]  # in chain order

    # 5. tenant B now has work already done
    st = client.get("/onboarding/status").json()
    assert st["jobs"] == {**st["jobs"], "queued": 0, "running": 0, "done": 5, "failed": 0, "last_error": None}
    assert st["first_pulse_ready"] is True and st["steps"]["pulse"] is True
    assert all(j["status"] == "done" and j["attempts"] == 1 for j in st["jobs"]["chain"])
    backfill = st["jobs"]["chain"][0]
    assert backfill["source"] == "linear" and backfill["result"]["issues"]["created"] == 9
    b = {t: conn.execute(f"SELECT count(*) AS n FROM {t} WHERE tenant_id = %s", (TB,)).fetchone()["n"] for t in COUNTED}
    assert b["issues"] == 9 and b["projects"] == 3
    assert b["signals"] > 0 and b["context_packs"] == 1
    pulse = conn.execute(
        "SELECT * FROM runs WHERE tenant_id = %s AND skill = %s ORDER BY started_at DESC", (TB, PULSE)
    ).fetchall()
    # the ledger explains it: the Tier-2 call was refused (no credentials), then the Tier-0 fallback ran
    assert [(p["tier"], p["status"]) for p in pulse] == [(0, "degraded"), (2, "degraded")]
    assert pulse[0]["outcome"].startswith("Morning pulse (Tier 0)") and pulse[1]["outcome"].startswith("refused:")
    cos = conn.execute(
        "SELECT tier, status, outcome FROM runs WHERE tenant_id = %s AND skill = 'cockpit.chief_of_staff' ORDER BY started_at DESC",
        (TB,),
    ).fetchall()
    assert cos and cos[0]["tier"] == 0 and cos[0]["outcome"].startswith("Chief of Staff (Tier 0")
    ck = client.get("/cockpit").json()
    assert ck["pulse"]["source"] == "run" and ck["pulse"]["text"] == pulse[0]["outcome"]
    assert [j["kind"] for j in client.get("/onboarding/jobs").json()] == [
        "morning_pulse",
        "chief_of_staff",
        "context_pack",
        "signals",
        "backfill:linear",
    ]

    # 6. every other tenant is exactly where it was
    assert counts_except(conn, TB) == before
    assert not conn.execute("SELECT 1 FROM issues WHERE id = 'ACM-158' AND tenant_id <> %s", (TB,)).fetchone()

    # 7. after the chain finished, compile queues a fresh one (the previous rows stay as history)
    fresh = client.post("/onboarding/compile").json()
    assert all(j["enqueued"] and j["status"] == "queued" for j in fresh["jobs"])
    assert jobs.drain(dsn=APP_TEST_DSN)["done"] == 5
    assert client.get("/onboarding/status").json()["jobs"]["done"] == 10


# --- queue rules ----------------------------------------------------------------------------------------------

TQ1, TQ2 = "q-one", "q-two"


@pytest.fixture()
def queue_tenants(conn, no_model):
    for t in (TQ1, TQ2):
        wipe_tenant(conn, t)
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s)", (t, t))
    conn.commit()
    yield
    for t in (TQ1, TQ2):
        wipe_tenant(conn, t)


def job_rows(conn, tenant_id: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, kind, status, attempts, error, run_after FROM tenant_jobs WHERE tenant_id = %s ORDER BY created_at, id",
            (tenant_id,),
        ).fetchall()
    ]


def test_enqueue_chain_is_ordered_and_idempotent(conn, queue_tenants):
    with tenant_conn(TQ1, APP_TEST_DSN) as c:
        c.execute(
            "INSERT INTO connections (id, tenant_id, source, secret_ref, status) VALUES "
            "('q1-brex', %s, 'brex', 'kv:brex_api_key', 'connected'), "
            "('q1-linear', %s, 'linear', 'kv:linear_api_key', 'connected'), "
            "('q1-slack', %s, 'slack', 'kv:slack_api_key', 'disabled'), "
            "('q1-apollo', %s, 'apollo', 'kv:apollo_api_key', 'connected')",  # no ingest module yet → no backfill
            (TQ1, TQ1, TQ1, TQ1),
        )
        first = queue.enqueue_chain(c, TQ1)
        assert [j["kind"] for j in first] == ["backfill:linear", "backfill:brex", *queue.TAIL_KINDS]
        assert all(j["enqueued"] for j in first)
        assert first[0]["payload"] == {"source": "linear"} and first[2]["payload"] == {}
        second = queue.enqueue_chain(c, TQ1)
        assert not any(j["enqueued"] for j in second) and [j["id"] for j in second] == [j["id"] for j in first]
        summary = queue.status_summary(c, TQ1)
        assert summary["queued"] == 6 and summary["last_error"] is None and len(summary["chain"]) == 6
    assert len(job_rows(conn, TQ1)) == 6


def test_jobs_run_in_order_per_tenant_and_interleave_across_tenants(conn, queue_tenants, monkeypatch):
    seen: list[tuple[str, str]] = []

    def fake(kind):
        def handler(c, tid, payload):
            seen.append((tid, kind))
            return {"ok": True}

        return handler

    monkeypatch.setattr(jobs, "handler_for", lambda kind: fake(kind))
    with tenant_conn(TQ1, APP_TEST_DSN) as c:
        queue.enqueue_chain(c, TQ1)  # no connections → the four tail kinds
    with tenant_conn(TQ2, APP_TEST_DSN) as c:
        queue.enqueue_chain(c, TQ2)

    # one pass, one claim at a time: every claim is the oldest runnable job whose tenant has nothing earlier open
    one = jobs.service_jobs(dsn=APP_TEST_DSN, max_jobs=1)
    assert one["claimed"] == 1 and one["jobs"][0] == {**one["jobs"][0], "tenant_id": TQ1, "kind": "signals"}
    rest = jobs.drain(dsn=APP_TEST_DSN)
    assert rest["claimed"] == 7 and rest["done"] == 7
    for tid in (TQ1, TQ2):
        assert [k for t, k in seen if t == tid] == list(queue.TAIL_KINDS)
    # a pinned service only touches its tenants
    with tenant_conn(TQ2, APP_TEST_DSN) as c:
        queue.enqueue_chain(c, TQ2)
    assert jobs.service_jobs([TQ1], dsn=APP_TEST_DSN)["claimed"] == 0
    assert jobs.drain([TQ2], dsn=APP_TEST_DSN)["done"] == 4


def test_failed_job_retries_three_times_with_backoff_and_the_chain_continues(conn, queue_tenants, monkeypatch):
    calls = {"n": 0}

    def handler_for(kind):
        if kind == "backfill:linear":

            def boom(c, tid, payload):
                calls["n"] += 1
                c.execute("INSERT INTO issues (tenant_id, id, title) VALUES (%s, 'PARTIAL', 'rolled back')", (tid,))
                raise RuntimeError("linear says no")

            return boom
        return lambda c, tid, payload: {"ok": True}

    monkeypatch.setattr(jobs, "handler_for", handler_for)
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (3600, 3600))
    with tenant_conn(TQ1, APP_TEST_DSN) as c:
        c.execute(
            "INSERT INTO connections (id, tenant_id, source, secret_ref, status) VALUES ('q1-linear', %s, 'linear', 'kv:linear_api_key', 'connected')",
            (TQ1,),
        )
        queue.enqueue_chain(c, TQ1)

    # attempt 1 fails → re-queued with backoff; nothing later may run while it is queued (per-tenant order)
    out = jobs.drain(dsn=APP_TEST_DSN)
    assert out["claimed"] == 1 and out["queued"] == 1 and calls["n"] == 1
    rows = job_rows(conn, TQ1)
    assert rows[0]["status"] == "queued" and rows[0]["attempts"] == 1 and "linear says no" in rows[0]["error"]
    assert rows[0]["run_after"] > datetime.now(UTC) + timedelta(minutes=30)
    assert all(r["status"] == "queued" and r["attempts"] == 0 for r in rows[1:])
    assert jobs.service_jobs(dsn=APP_TEST_DSN)["claimed"] == 0  # backoff holds
    assert not conn.execute("SELECT 1 FROM issues WHERE tenant_id = %s AND id = 'PARTIAL'", (TQ1,)).fetchone()

    # backoff elapsed twice more → attempts 2 and 3, then 'failed'; the chain continues past it
    for expected_attempts in (2, 3):
        conn.execute(
            "UPDATE tenant_jobs SET run_after = now() WHERE tenant_id = %s AND kind = 'backfill:linear'", (TQ1,)
        )
        conn.commit()
        out = jobs.service_jobs(dsn=APP_TEST_DSN)
        assert calls["n"] == expected_attempts
        if expected_attempts < jobs.MAX_ATTEMPTS:
            assert out == {**out, "claimed": 1, "queued": 1}
        else:
            assert out["claimed"] == 5 and out["failed"] == 1 and out["done"] == 4
    rows = job_rows(conn, TQ1)
    assert rows[0]["status"] == "failed" and rows[0]["attempts"] == 3
    assert [r["status"] for r in rows[1:]] == ["done"] * 4
    with tenant_conn(TQ1, APP_TEST_DSN) as c:
        summary = queue.status_summary(c, TQ1)
    assert summary["failed"] == 1 and summary["done"] == 4 and "linear says no" in summary["last_error"]
    # a real backfill with no stored credential (or no ingest module) is a BackfillFailed, never a secret in the text
    with tenant_conn(TQ1, APP_TEST_DSN) as c:
        with pytest.raises(jobs.BackfillFailed, match="no credential"):
            jobs.run_backfill(c, TQ1, {"source": "linear"})
        with pytest.raises(jobs.BackfillFailed, match="no ingest module"):
            jobs.run_backfill(c, TQ1, {"source": "apollo"})


def test_tenant_jobs_are_isolated_under_rls_but_claimable_across_tenants(conn, queue_tenants):
    with tenant_conn(TQ1, APP_TEST_DSN) as c:
        queue.enqueue(c, TQ1, "signals")
    with tenant_conn(TQ1, APP_TEST_DSN) as c, pytest.raises(Exception):
        queue.enqueue(c, TQ2, "signals")  # WITH CHECK: the app role bound to q-one cannot queue for q-two
    with tenant_conn(TQ2, APP_TEST_DSN) as c:
        assert c.execute("SELECT count(*) AS n FROM tenant_jobs").fetchone()["n"] == 0
        queue.enqueue(c, TQ2, "signals")
        assert [r["tenant_id"] for r in c.execute("SELECT tenant_id FROM tenant_jobs")] == [TQ2]
    # an anonymous app connection sees no rows on the table, yet claims through the SECURITY DEFINER function
    from common.tenants import anonymous_conn

    with anonymous_conn(APP_TEST_DSN) as c:
        assert c.execute("SELECT count(*) AS n FROM tenant_jobs").fetchone()["n"] == 0
        first = jobs.claim_next(c)
        second = jobs.claim_next(c)
        third = jobs.claim_next(c)
    assert first and second and third is None
    assert {first["tenant_id"], second["tenant_id"]} == {TQ1, TQ2}
    assert first["status"] == "running" and first["attempts"] == 1
    conn.execute("UPDATE tenant_jobs SET status = 'done' WHERE tenant_id = ANY(%s)", ([TQ1, TQ2],))
    conn.commit()
