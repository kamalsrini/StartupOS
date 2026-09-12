"""Track B end-to-end: seed → assign_owner proposes → founder approves → executor lands it in (fake) Linear.

Also: the scheduler's 15-minute tick over the same seed, and the Slack gateway command path. No network.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from track_b_fakes import (
    TENANT,
    FakeAnthropic,
    FakeLinearGraphQL,
    FakeSlackClient,
    reset_tenant,
    seed_bill,
    seed_issue,
    seed_pack,
    seed_signal,
    seed_team_doc,
)

from common.models import Decision
from daemon import approvals, budget, executors, llm, scheduler
from daemon.executors import linear, slack
from daemon.gateway import slack as gateway
from daemon.skills import build_ctx
from daemon.skills.build import assign_owner

pytestmark = pytest.mark.functional

TEAM_MD = "# Team\n- Alexey — backend, infra, workspace-graph\n- Maria — frontend, web, onboarding\n"


def _seed(conn):
    reset_tenant(conn)
    seed_team_doc(conn, TEAM_MD)
    seed_pack(conn)
    seed_issue(conn, "UNI-158", "workspace-graph build is 24 min per parent", labels=["Bug"])
    seed_issue(conn, "UNI-161", "fixer picks files_changed[0] as primary", assignee="Alexey")
    seed_issue(
        conn, "UNI-170", "Onboarding web flow stuck", assignee="Maria", status="In Progress", updated_days_ago=10
    )
    seed_issue(conn, "UNI-201", "JCI: multi-repo support", priority=3, labels=["account:jci"])
    seed_signal(conn, "build.unassigned_high", "UNI-158")
    seed_signal(conn, "build.stale_in_progress", "UNI-170", severity="medium", kind="open")
    seed_signal(conn, "customers.ask_untouched", "UNI-201", module="customers", kind="reply")
    seed_bill(conn, "b1", "Contractor One", "12500.00", due_in_days=4)
    seed_signal(conn, "finance.bill_due_7d", "b1", module="finance", kind="reply")


def test_assign_owner_end_to_end(conn, monkeypatch):
    _seed(conn)
    gql = FakeLinearGraphQL()
    monkeypatch.setattr(linear, "_graphql", gql)

    # 1. skill proposes
    out = assign_owner.run(build_ctx(conn, TENANT, now=datetime(2026, 9, 3, 12, tzinfo=UTC)))
    assert [a.id for a in out] == ["assign-uni-158"]
    row = conn.execute("SELECT * FROM approvals WHERE id = 'assign-uni-158'").fetchone()
    assert row["status"] == "pending" and row["exec"]["input"] == {
        "id": "UNI-158",
        "assignee": "Alexey",
        "dueDate": "2026-09-10",
    }
    assert row["signal_id"] == "build.unassigned_high:UNI-158" and row["created_by_run"]

    # 2. executor refuses while pending
    with pytest.raises(executors.NotApproved):
        executors.run_approved(conn, "assign-uni-158")

    # 3. founder approves
    decided = approvals.decide(conn, "assign-uni-158", Decision(decision="approve", decided_by="kamal"))
    assert decided.status == "approved" and decided.decided_by == "kamal"

    # 4. executor runs against the fake Linear client
    done = executors.run_approved(conn, "assign-uni-158")
    assert done["status"] == "executed"
    assert done["result"]["url"] == "https://linear.app/unitone/issue/UNI-158"
    assert done["result"]["text"].startswith("Updated UNI-158")
    update = next(v for q, v in gql.mutations if "issueUpdate" in q)
    assert update["input"] == {"dueDate": "2026-09-10", "assigneeId": "user-uuid-alexey"}

    # 5. ledger: one Tier-0 run for the proposal, one for the execution; nothing charged to the budget
    runs = conn.execute(
        "SELECT skill, tier, status FROM runs WHERE tenant_id = %s ORDER BY started_at", (TENANT,)
    ).fetchall()
    assert [(r["skill"], r["tier"], r["status"]) for r in runs] == [
        ("build.assign_owner", 0, "ok"),
        ("exec.Linear.save_issue", 0, "ok"),
    ]
    assert conn.execute("SELECT count(*) AS n FROM budgets WHERE tenant_id = %s", (TENANT,)).fetchone()["n"] == 0

    # 6. once executed, it can never run again
    with pytest.raises(executors.NotApproved):
        executors.run_approved(conn, "assign-uni-158")


def test_scheduler_tick_runs_signal_skills_and_executes_approved(conn, monkeypatch):
    _seed(conn)
    gql, sc, fake = FakeLinearGraphQL(), FakeSlackClient(), FakeAnthropic(text="Hi Maria, is UNI-170 blocked?")
    monkeypatch.setattr(linear, "_graphql", gql)
    monkeypatch.setattr(slack, "_client_factory", lambda: sc)
    monkeypatch.setattr(llm, "_client_factory", lambda: fake)

    # make the scheduler's jobs use the test connection
    from contextlib import contextmanager

    @contextmanager
    def test_conn():
        yield conn

    monkeypatch.setattr(scheduler, "get_conn", test_conn)

    results = scheduler.run_signal_skills(TENANT)
    ids = sorted(a.id for out in results.values() for a in out)
    assert ids == ["ap-b1", "assign-uni-158", "nudge-uni-170", "prioritize-uni-201"]
    pending = approvals.list_pending(conn, TENANT)
    assert len(pending) == 4 and scheduler.run_approved(TENANT) == []

    # approve everything through the Slack gateway text path, then execute
    approvals.decide(conn, "assign-uni-158", Decision(decision="approve"))
    approvals.decide(conn, "prioritize-uni-201", Decision(decision="approve"))
    approvals.decide(conn, "ap-b1", Decision(decision="approve"))  # record-only → decisions.md
    reply = gateway.handle_text(conn, TENANT, "decline nudge-uni-170 Maria is out this week")
    assert reply.startswith("Declined")

    done = scheduler.run_approved(TENANT)
    assert {r["id"]: r["status"] for r in done} == {
        "assign-uni-158": "executed",
        "prioritize-uni-201": "executed",
        "ap-b1": "executed",
    }
    assert len(gql.mutations) == 2 and sc.posted == []  # nudge was declined, nothing posted
    doc = conn.execute(
        "SELECT content FROM brain_docs WHERE tenant_id = %s AND path = 'decisions.md'", (TENANT,)
    ).fetchone()["content"]
    assert "Approved · Brex · pay" in doc and "Maria is out this week" in doc

    # a second tick is a no-op: proposals are idempotent, nothing left approved
    again = scheduler.run_signal_skills(TENANT)
    assert all(out == [] for out in again.values()) and scheduler.run_approved(TENANT) == []
    assert len(fake.calls) == 1  # the nudge draft was the only Tier-2 call
    b = budget.get_or_create(conn, TENANT)
    assert b["tier2_tokens_used"] == 9_200 and b["state"] == "normal"


def test_gateway_approve_executes_immediately(conn, monkeypatch):
    _seed(conn)
    gql = FakeLinearGraphQL()
    monkeypatch.setattr(linear, "_graphql", gql)
    assign_owner.run(build_ctx(conn, TENANT))
    listing = gateway.handle_text(conn, TENANT, "pending")
    assert "assign-uni-158" in listing
    reply = gateway.handle_text(conn, TENANT, "approve assign-uni-158", user="U_KAMAL")
    assert reply.startswith("Approved and executed") and "linear.app/unitone/issue/UNI-158" in reply
    row = conn.execute("SELECT status, decided_by FROM approvals WHERE id = 'assign-uni-158'").fetchone()
    assert row["status"] == "executed" and row["decided_by"] == "U_KAMAL"
    assert "not pending" in gateway.handle_text(conn, TENANT, "approve assign-uni-158")


def test_scheduler_builds_jobs_in_tenant_timezone(monkeypatch):
    monkeypatch.setattr(scheduler, "tenant_timezone", lambda t: "America/Los_Angeles")
    sched = scheduler.build_scheduler(TENANT)
    assert sched is not None
    jobs = {j.id: str(j.trigger) for j in sched.get_jobs()}
    assert set(jobs) == {
        "chief_of_staff",
        "morning_pulse",
        "evening_digest",
        "context_pack",
        "tick_15m",
        "service_asks",
        "weekly_review",
    }
    assert "hour='7'" in jobs["morning_pulse"] and "hour='18'" in jobs["evening_digest"]
    assert "hour='6'" in jobs["chief_of_staff"] and "minute='30'" in jobs["chief_of_staff"]
    assert "minute='*/15'" in jobs["tick_15m"] and "day_of_week='fri'" in jobs["weekly_review"]


def test_tick_runs_signal_engine_before_skills(conn, monkeypatch):
    """Spec audit 2026-09-12: signals must be computed by the daemon itself, not by a manual `make signals`."""
    import os

    from common.db import get_conn as _get_conn

    conn.execute("DELETE FROM issues WHERE id = 'TICK-1'")
    conn.execute(
        "INSERT INTO issues (tenant_id, id, title, status, status_type, priority) VALUES ('unitone','TICK-1','tick test',"
        "'Backlog','backlog',2)"
    )
    conn.execute("DELETE FROM signals WHERE id = 'build.unassigned_high:TICK-1'")
    conn.commit()
    dsn = os.environ.get("STARTUPOS_TEST_DSN", "postgresql://postgres@localhost:5432/startupos_test")
    monkeypatch.setattr(scheduler, "get_conn", lambda **kw: _get_conn(dsn, tenant_id="unitone"))
    scheduler.run_signal_engine("unitone")
    row = conn.execute(
        "SELECT id FROM signals WHERE id = 'build.unassigned_high:TICK-1' AND resolved_at IS NULL"
    ).fetchone()
    assert row is not None
    conn.execute("DELETE FROM issues WHERE id = 'TICK-1'")
    conn.execute("DELETE FROM signals WHERE id = 'build.unassigned_high:TICK-1'")
    conn.commit()
