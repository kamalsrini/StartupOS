"""API endpoint tests with FastAPI TestClient against the scratch Postgres DB (STARTUPOS_API_DSN = test DSN)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.conftest import login_as

pytestmark = pytest.mark.functional

T = "trackc-api"  # dedicated tenant so this file never touches other tracks' rows
TABLES = (
    "approvals",
    "signals",
    "runs",
    "budgets",
    "issues",
    "projects",
    "deployments",
    "bills",
    "vendors",
    "accounts_bank",
    "transactions",
    "cards",
    "sequences",
    "accounts",
    "messages",
    "documents",
    "brain_docs",
    "connections",
    "users",
)


@pytest.fixture()
def client(conn, auth_env):
    """TestClient signed in as the owner of tenant T (session cookie). `conn` seeds; commit before hitting the API."""
    from common.db import ensure_tenant

    ensure_tenant(conn, T, "Track C API", "https://trackc.test")
    for t in ("sessions", "api_tokens"):
        conn.execute(f"DELETE FROM {t} WHERE tenant_id = %s", (T,))
    for t in TABLES:
        conn.execute(f"DELETE FROM {t} WHERE tenant_id = %s", (T,))
    conn.commit()
    user_id, cookie = login_as(conn, T, "owner@trackc.test", "Track C Owner")
    from api.main import app

    with TestClient(app) as c:
        c.cookies.set("sos_session", cookie)
        c.user_id = user_id
        yield c


def seed(conn, sql: str, params=()):
    conn.execute(sql, params)
    conn.commit()


# --- health -------------------------------------------------------------------


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["db"] == "up" and body["tenant"] == T
    assert "postgresql://" not in r.text  # never echo the DSN


# --- modules -----------------------------------------------------------------


def test_modules_build_empty_db_has_four_honest_tiles(client):
    r = client.get("/modules/build")
    assert r.status_code == 200
    m = r.json()
    assert m["name"] == "build" and m["live"] is False
    assert len(m["tiles"]) == 4
    assert [t["val"] for t in m["tiles"][:3]] == ["0", "0", "0"]
    assert m["tiles"][3]["val"] == "—" and "not connected" in m["tiles"][3]["sub"]
    assert m["signals"] == [] and m["approvals"] == []
    assert m["memory"] == ""


@pytest.mark.parametrize(
    "name", ["build", "marketing", "web", "customers", "social", "security", "commerce", "research", "sales", "cockpit"]
)
def test_every_module_has_exactly_four_tiles(client, name):
    r = client.get(f"/modules/{name}")
    assert r.status_code == 200, r.text
    assert len(r.json()["tiles"]) == 4


def test_unknown_module_404(client):
    assert client.get("/modules/nope").status_code == 404


def test_modules_build_counts_and_sanitized_table(client, conn):
    seed(
        conn,
        """INSERT INTO issues (tenant_id, id, title, status, status_type, priority, assignee, project, labels, url) VALUES
           (%s, 'UNI-1', 'Urgent unassigned', 'Backlog', 'backlog', 1, NULL, 'Alpha', '{}', 'https://linear.app/x/UNI-1'),
           (%s, 'UNI-2', 'High assigned', 'In Progress', 'started', 2, 'Alexey', 'Alpha', '{}', NULL),
           (%s, 'UNI-3', 'Done thing', 'Done', 'completed', 3, 'Alexey', 'Alpha', '{}', NULL)""",
        (T, T, T),
    )
    seed(
        conn,
        """INSERT INTO projects (tenant_id, id, name, status, url, updated_at) VALUES
           (%s, 'p1', 'Alpha', 'In Progress', 'https://linear.app/x/project/alpha', now()),
           (%s, 'p2', '<b>Evil</b>', 'Planned', 'javascript:alert(1)', now())""",
        (T, T),
    )
    seed(
        conn,
        """INSERT INTO deployments (tenant_id, id, project, state, target, commit_message, created_at, url) VALUES
           (%s, 'd1', 'site', 'READY', 'production', 'ship it', now(), 'https://vercel.com/x/site')""",
        (T,),
    )
    m = client.get("/modules/build").json()
    tiles = {t["label"]: t for t in m["tiles"]}
    assert tiles["Open issues"]["val"] == "2"
    assert tiles["Urgent / High"]["val"] == "2" and tiles["Urgent / High"]["cls"] == "warn"
    assert "1 unassigned" in tiles["Urgent / High"]["sub"]
    assert tiles["In progress"]["val"] == "1"
    assert tiles["Prod deploys"]["val"] == "1 / 1"
    rows = m["table"]["rows"]
    assert any("&lt;b&gt;Evil&lt;/b&gt;" in r[0] for r in rows)  # escaped
    for r in rows:
        for cell in r:
            assert "javascript:" not in cell
            assert (
                cell == ""
                or "<" not in cell
                or cell.startswith('<span class="status-pill status-')
                or cell.startswith('<a href="http')
            )
    assert m["table2"]["rows"][0][0] == "site"


def test_module_signals_link_pending_approval(client, conn):
    seed(
        conn,
        """INSERT INTO signals (id, tenant_id, module, rule_id, severity, kind, title, meta, entity, entity_id)
           VALUES ('build.unassigned_high:UNI-1', %s, 'build', 'build.unassigned_high', 'high', 'click', 'UNI-1 unassigned', 'High · unassigned', 'issues', 'UNI-1')""",
        (T,),
    )
    seed(
        conn,
        """INSERT INTO approvals (id, tenant_id, module, type, target, preview, exec, status, signal_id)
           VALUES ('assign-uni-1', %s, 'build', 'Linear · assign', 'UNI-1 → Alexey', 'Assign it', %s, 'pending', 'build.unassigned_high:UNI-1')""",
        (T, json.dumps({"server": "Linear", "tool": "save_issue", "input": {"id": "UNI-1"}})),
    )
    m = client.get("/modules/build").json()
    assert len(m["signals"]) == 1
    s = m["signals"][0]
    assert s["approval_id"] == "assign-uni-1" and s["action"] == "Assign" and s["kind"] == "click"
    assert len(m["approvals"]) == 1 and m["approvals"][0]["exec"]["server"] == "Linear"


def test_module_memory_is_brain_slice_prefix(client, conn):
    seed(
        conn,
        "INSERT INTO brain_docs (tenant_id, path, slice, content, source) VALUES (%s, 'build.md', 'build', %s, 'human')",
        (T, "Team: Alexey · " + "x" * 500),
    )
    m = client.get("/modules/build").json()
    assert m["memory"].startswith("Team: Alexey") and len(m["memory"]) == 300


# --- finance -----------------------------------------------------------------


def test_finance_shapes_and_vendor_has_no_bank_fields(client, conn):
    seed(
        conn,
        """INSERT INTO accounts_bank (tenant_id, id, name, nickname, account_type, priority, last4, available, inflow_mtd, outflow_mtd)
           VALUES (%s, 'acc1', 'Checking', 'Primary checking', 'CHECKING', 'PRIMARY', '7355', 517.26, 0, 0)""",
        (T,),
    )
    seed(
        conn,
        """INSERT INTO vendors (tenant_id, id, name, email, rail, country, status)
           VALUES (%s, 'v1', 'Devpulse LLC', 'v@devpulse.com', 'INTL_SWIFT_WIRE', 'UA', 'ACTIVE')""",
        (T,),
    )
    seed(
        conn,
        """INSERT INTO bills (tenant_id, id, vendor_id, vendor_name, amount, currency, status, payment_status, due_at, invoice_number)
           VALUES (%s, 'b1', 'v1', 'Devpulse LLC', 6500, 'USD', 'APPROVED', 'AWAITING_PAYMENT', now() + interval '3 days', 'INV-1'),
                  (%s, 'b2', 'v1', 'Devpulse LLC', 12500, 'USD', 'APPROVED', 'CLEARED', now() - interval '30 days', 'INV-0')""",
        (T, T),
    )
    seed(
        conn,
        """INSERT INTO transactions (tenant_id, id, type, status, amount, currency, counterparty, occurred_at)
           VALUES (%s, 't1', 'WIRE', 'PROCESSED', 2500, 'USD', 'PRASHANT KETKAR', now() - interval '1 day'),
                  (%s, 't2', 'WIRE', 'PROCESSED', -100.5, 'USD', 'AWS', now() - interval '2 days')""",
        (T, T),
    )
    seed(
        conn,
        """INSERT INTO cards (tenant_id, id, holder, display_name, last4, status, limit_total, limit_spent)
           VALUES (%s, 'c1', 'Prashant Ketkar', 'Employee Card', '6445', 'ACTIVE', 100000, 0)""",
        (T,),
    )
    r = client.get("/finance")
    assert r.status_code == 200
    f = r.json()
    assert set(f) == {"snapshot_at", "live", "accounts", "bills", "vendors", "cards", "expenses", "transactions"}
    assert f["live"] is False and f["expenses"] == []
    acc = f["accounts"][0]
    assert acc["balance_breakdown"]["available_balance"] == "517.26 USD"
    assert acc["account_number_last_four"] == "7355"
    vendor = f["vendors"][0]
    assert set(vendor) == {"id", "name", "status", "email", "rail", "country"}
    assert f["bills"][0]["amount"] in ("6500.00", "12500.00")
    tx = {t["id"]: t for t in f["transactions"]}
    assert tx["t1"]["amount"] == "2500.00 USD" and tx["t1"]["display_name"] == "PRASHANT KETKAR"
    assert tx["t2"]["amount"] == "-100.50 USD"
    assert "timestamp" in tx["t1"]
    card = f["cards"][0]
    assert card["holder_name"] == "Prashant Ketkar" and card["limit"]["total"]["quantity"] == "100000.00"

    s = client.get("/finance/summary").json()
    assert s["cash_on_hand"] == "517.26"
    assert s["ap_next_7d"] == "6500.00"
    assert s["received_mtd"] in ("2500.00", "0.00")  # depends on where in the month 'now' falls
    assert s["last_inflow_at"] is not None


def test_finance_empty_db_is_zero_not_invented(client):
    f = client.get("/finance").json()
    assert f["accounts"] == [] and f["bills"] == [] and f["vendors"] == [] and f["transactions"] == []
    s = client.get("/finance/summary").json()
    assert s == {
        "cash_on_hand": "0.00",
        "ap_next_7d": "0.00",
        "ap_next_7d_count": 0,
        "received_mtd": "0.00",
        "received_mtd_count": 0,
        "outflow_mtd": "0.00",
        "last_inflow_at": None,
        "accounts": 0,
        "currency": "USD",
    }


# --- approvals ---------------------------------------------------------------


def _seed_approval(conn, aid="assign-uni-158", status="pending"):
    seed(
        conn,
        """INSERT INTO approvals (id, tenant_id, module, type, target, preview, exec, status)
           VALUES (%s, %s, 'build', 'Linear · assign', 'UNI-158 → Alexey', 'Assign UNI-158 to Alexey', %s, %s)""",
        (
            aid,
            T,
            json.dumps({"server": "Linear", "tool": "save_issue", "input": {"id": "UNI-158", "assignee": "Alexey"}}),
            status,
        ),
    )


def test_approvals_list_filters(client, conn):
    _seed_approval(conn, "a1")
    _seed_approval(conn, "a2", status="executed")
    assert [a["id"] for a in client.get("/approvals").json()] == ["a1"]
    assert [a["id"] for a in client.get("/approvals?status=executed").json()] == ["a2"]
    assert client.get("/approvals?module=finance").json() == []
    assert len(client.get("/approvals?status=").json()) == 2


def test_approve_happy_path_then_409(client, conn):
    _seed_approval(conn)
    r = client.post("/approvals/assign-uni-158/decide", json={"decision": "approve", "decided_by": "kamal"})
    assert r.status_code == 200, r.text
    a = r.json()
    # decided_by is the authenticated user's id — the body value is ignored
    assert a["status"] == "approved" and a["decided_by"] == client.user_id and a["decided_at"]
    assert a["result"] is None  # execution is the daemon's job
    assert a["exec"]["tool"] == "save_issue"
    r2 = client.post("/approvals/assign-uni-158/decide", json={"decision": "decline", "reason": "changed my mind"})
    assert r2.status_code == 409
    row = conn.execute("SELECT status FROM approvals WHERE id='assign-uni-158' AND tenant_id=%s", (T,)).fetchone()
    assert row["status"] == "approved"


def test_decline_with_reason_and_edited_preview(client, conn):
    _seed_approval(conn, "a-edit")
    r = client.post(
        "/approvals/a-edit/decide",
        json={"decision": "decline", "reason": "Alexey is on leave", "edited_preview": "Assign to Manmeet instead"},
    )
    assert r.status_code == 200
    a = r.json()
    assert a["status"] == "declined" and a["decline_reason"] == "Alexey is on leave"
    assert a["preview"] == "Assign to Manmeet instead"


def test_decide_missing_404(client):
    assert client.post("/approvals/nope/decide", json={"decision": "approve"}).status_code == 404


# --- cockpit / runs / ask ----------------------------------------------------


def test_cockpit_fallback_pulse_and_tiles(client, conn):
    c = client.get("/cockpit").json()
    assert c["pulse"]["source"] == "fallback" and c["pulse"]["tier"] == 0
    assert len(c["tiles"]) == 4
    assert c["pending_approvals"] == 0
    assert c["spend"]["runs"] == 0 and c["spend"]["cost_usd"] == "0.00000"
    # a real pulse run wins over the fallback
    seed(
        conn,
        """INSERT INTO runs (id, tenant_id, trigger, skill, tier, model, tokens_in, tokens_out, cost_usd, outcome)
           VALUES ('run_1', %s, 'schedule', 'cockpit.morning_pulse', 2, 'claude-sonnet-4-5', 1000, 200, 0.0123, 'Thu Sep 4. Three things need you.')""",
        (T,),
    )
    c = client.get("/cockpit").json()
    assert c["pulse"]["source"] == "run" and c["pulse"]["text"].startswith("Thu Sep 4")
    s = client.get("/runs/summary").json()
    assert s["runs"] == 1 and s["by_skill"][0]["skill"] == "cockpit.morning_pulse" and s["by_tier"][0]["tier"] == 2
    assert s["cost_usd"] == "0.01230"
    assert client.get("/runs/summary?month=1999-01").json()["runs"] == 0
    assert client.get("/runs/summary?month=bad").status_code == 422


def test_ask_is_fts_retrieval_only(client, conn):
    seed(
        conn,
        "INSERT INTO brain_docs (tenant_id, path, slice, content, source) VALUES (%s, 'pricing.md', 'pricing', 'Founder tier is $99 per month, flat pricing, no credits.', 'human')",
        (T,),
    )
    seed(
        conn,
        "INSERT INTO messages (tenant_id, id, source, channel, author, text, occurred_at) VALUES (%s, '1.0', 'slack', 'sales', 'kamal', 'JCI asked about pricing for the POC', %s)",
        (T, datetime.now(tz=UTC) - timedelta(days=1)),
    )
    r = client.get("/ask", params={"q": "pricing"})
    assert r.status_code == 200
    body = r.json()
    assert body["answered_by"] is None
    sources = {h["source"] for h in body["hits"]}
    assert "brain_docs" in sources and "messages:slack" in sources
