"""daemon.skills — assign_owner mapping, proposal shapes, evening_digest Tier-0 fallback, ask retrieval, gateway parsing."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from track_b_fakes import (
    TENANT,
    FakeAnthropic,
    reset_tenant,
    seed_bill,
    seed_issue,
    seed_pack,
    seed_signal,
    seed_team_doc,
)

from daemon import llm, skills
from daemon.gateway import slack as gateway
from daemon.skills import build_ctx
from daemon.skills.ask import answer as ask
from daemon.skills.build import assign_owner, nudge_stale
from daemon.skills.cockpit import evening_digest, morning_pulse
from daemon.skills.customers import prioritize_ask
from daemon.skills.finance import ap_queue

TEAM_MD = """# Team

- **Alexey** (CTO) — backend, infra, workspace-graph, fixer, api
- Maria: frontend, web, dashboard, onboarding
| Sam | GTM | apollo, outbound, sequences |
"""

# --- pure ---------------------------------------------------------------------------------------------


def test_registry_has_v1_skills():
    assert set(skills.REGISTRY) == {
        "cockpit.morning_pulse",
        "cockpit.evening_digest",
        "build.assign_owner",
        "build.nudge_stale",
        "finance.ap_queue",
        "customers.prioritize_ask",
        "ask.answer",
    }
    assert skills.get("cockpit.morning_pulse").high_priority is True
    assert skills.get("build.nudge_stale").tier == 2 and skills.get("finance.ap_queue").tier == 0
    with pytest.raises(KeyError):
        skills.get("brex.pay")


def test_parse_aors_tolerates_bullets_colons_and_tables():
    aors = assign_owner.parse_aors(TEAM_MD)
    assert [n for n, _ in aors] == ["Alexey", "Maria", "Sam"]
    assert aors[0][1] == ["backend", "infra", "workspace-graph", "fixer", "api"]
    assert "frontend" in aors[1][1] and "apollo" in aors[2][1]


def test_pick_owner_by_keyword_hits():
    aors = assign_owner.parse_aors(TEAM_MD)
    assert assign_owner.pick_owner("workspace-graph build is 24 min per parent", aors) == (
        "Alexey",
        ["workspace-graph"],
    )
    assert assign_owner.pick_owner("Dashboard onboarding flow broken on web", aors)[0] == "Maria"
    assert assign_owner.pick_owner("Something unrelated", aors) == ("Alexey", [])
    assert assign_owner.pick_owner("x", []) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("approve assign-uni-158", ("approve", ["assign-uni-158"])),
        ("<@U123> Approve nudge-uni-9", ("approve", ["nudge-uni-9"])),
        ("decline ap-b1 vendor already paid", ("decline", ["ap-b1", "vendor already paid"])),
        ("pending", ("pending", [])),
        ("what did we tell JCI about multi-repo?", ("ask", ["what did we tell JCI about multi-repo?"])),
        ("approve", ("ask", ["approve"])),
    ],
)
def test_gateway_parse_command(text, expected):
    assert gateway.parse_command(text) == expected


def test_gateway_does_not_start_without_tokens(monkeypatch):
    monkeypatch.delenv("SLACK_APP_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert gateway.available() is False and gateway.start() is None


# --- with Postgres ------------------------------------------------------------------------------------


@pytest.mark.functional
def test_assign_owner_proposal_shape(conn):
    reset_tenant(conn)
    seed_team_doc(conn, TEAM_MD)
    seed_issue(conn, "UNI-158", "workspace-graph build is 24 min per parent; corpus scale infeasible", labels=["Bug"])
    seed_issue(conn, "UNI-160", "Dashboard onboarding flow broken", labels=["Bug"])
    seed_issue(conn, "UNI-170", "Assigned already", assignee="Sam")
    sid = seed_signal(conn, "build.unassigned_high", "UNI-158")
    seed_signal(conn, "build.unassigned_high", "UNI-160")
    seed_signal(conn, "build.stale_in_progress", "UNI-170", severity="medium")

    ctx = build_ctx(conn, TENANT, now=datetime(2026, 9, 3, 12, tzinfo=UTC))
    out = assign_owner.run(ctx)
    assert [a.id for a in out] == ["assign-uni-158", "assign-uni-160"]
    a = out[0]
    assert a.status == "pending" and a.module == "build" and a.type == "Linear · assign"
    assert a.target == "UNI-158 → Alexey · due 2026-09-10"
    assert a.exec.server == "Linear" and a.exec.tool == "save_issue"
    assert a.exec.input == {"id": "UNI-158", "assignee": "Alexey", "dueDate": "2026-09-10"}
    assert a.signal_id == sid and "workspace-graph" in a.preview and "Rationale" in a.preview
    run = conn.execute("SELECT * FROM runs WHERE id = %s", (a.created_by_run,)).fetchone()
    assert run["tier"] == 0 and run["skill"] == "build.assign_owner" and run["trigger"] == "signal"
    assert out[1].exec.input["assignee"] == "Maria"
    # idempotent: second run proposes nothing new
    assert assign_owner.run(ctx) == []


@pytest.mark.functional
def test_assign_owner_without_team_doc_skips(conn):
    reset_tenant(conn)
    seed_signal(conn, "build.unassigned_high", "UNI-158")
    assert assign_owner.run(build_ctx(conn, TENANT)) == []
    run = conn.execute("SELECT * FROM runs WHERE tenant_id = %s", (TENANT,)).fetchone()
    assert run["status"] == "degraded"


@pytest.mark.functional
def test_prioritize_ask_proposal(conn):
    reset_tenant(conn)
    seed_issue(conn, "UNI-201", "JCI wants multi-repo support", priority=3, labels=["account:jci"])
    sid = seed_signal(conn, "customers.ask_untouched", "UNI-201", module="customers", kind="reply", meta="no update 4d")
    out = prioritize_ask.run(build_ctx(conn, TENANT, now=datetime(2026, 9, 3, tzinfo=UTC)))
    assert len(out) == 1 and out[0].id == "prioritize-uni-201" and out[0].module == "customers"
    assert out[0].exec.input == {"id": "UNI-201", "priority": 2, "dueDate": "2026-09-08"}
    assert out[0].signal_id == sid and "account:jci" in out[0].preview


@pytest.mark.functional
def test_ap_queue_record_only_with_brex_link(conn):
    reset_tenant(conn)
    seed_bill(conn, "b1", "Contractor One", "12500.00", due_in_days=3)
    seed_bill(conn, "b2", "Cleared Vendor", "100.00", due_in_days=1, payment_status="CLEARED")
    seed_bill(conn, "b3", "Later Vendor", "100.00", due_in_days=20)
    seed_bill(conn, "b4", "Overdue Vendor", "900.00", due_in_days=-2)
    seed_signal(conn, "finance.bill_due_7d", "b1", module="finance", kind="reply")
    out = ap_queue.run(build_ctx(conn, TENANT))
    assert sorted(a.id for a in out) == ["ap-b1", "ap-b4"]
    b1 = next(a for a in out if a.id == "ap-b1")
    assert b1.exec is None and b1.module == "finance" and b1.type == "Brex · pay"
    assert "https://dashboard.brex.com/bills" in b1.preview and "12500.00 USD" in b1.target
    assert b1.signal_id == "finance.bill_due_7d:b1"
    assert (
        next(a for a in out if a.id == "ap-b4").signal_id is None
        and "OVERDUE" in next(a for a in out if a.id == "ap-b4").target
    )
    assert ap_queue.run(build_ctx(conn, TENANT)) == []


@pytest.mark.functional
def test_evening_digest_tier0_fallback_without_key(conn, monkeypatch):
    reset_tenant(conn)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_client_factory", llm._default_client_factory)
    seed_signal(conn, "build.unassigned_high", "UNI-1")
    seed_signal(conn, "finance.bill_due_7d", "b1", module="finance", kind="reply")
    seed_bill(conn, "b1", "Contractor One", "12500.00", due_in_days=3)
    ap_queue.run(build_ctx(conn, TENANT))

    text = evening_digest.run(build_ctx(conn, TENANT))
    assert text.startswith("Evening digest")
    assert "1 approvals pending" in text and "2 signals open (2 high)" in text
    assert "build 1" in text and "finance 1" in text and "Brex · pay" in text
    runs = conn.execute(
        "SELECT * FROM runs WHERE tenant_id = %s AND skill = 'cockpit.evening_digest' ORDER BY started_at", (TENANT,)
    ).fetchall()
    assert [r["tier"] for r in runs] == [2, 0]  # the refused Tier-2 attempt, then the Tier-0 fallback
    assert runs[0]["status"] == "degraded" and runs[1]["outcome"] == text


@pytest.mark.functional
def test_evening_digest_tier0_fallback_when_exhausted(conn, monkeypatch):
    reset_tenant(conn)
    fake = FakeAnthropic(text="LLM digest")
    monkeypatch.setattr(llm, "_client_factory", lambda: fake)
    from daemon import budget

    b = budget.get_or_create(conn, TENANT)
    conn.execute(
        "UPDATE budgets SET tier2_tokens_used = tier2_tokens_allowed, state = 'exhausted' WHERE tenant_id = %s AND month = %s",
        (TENANT, b["month"]),
    )
    text = evening_digest.run(build_ctx(conn, TENANT))
    assert text.startswith("Evening digest") and fake.calls == []
    # conserve mode also degrades the (non-priority) digest
    conn.execute(
        "UPDATE budgets SET tier2_tokens_used = tier2_tokens_allowed * 0.95, state = 'conserve' WHERE tenant_id = %s AND month = %s",
        (TENANT, b["month"]),
    )
    assert evening_digest.run(build_ctx(conn, TENANT)).startswith("Evening digest") and fake.calls == []
    # normal budget → Tier 2 digest
    conn.execute(
        "UPDATE budgets SET tier2_tokens_used = 0, state = 'normal' WHERE tenant_id = %s AND month = %s",
        (TENANT, b["month"]),
    )
    assert evening_digest.run(build_ctx(conn, TENANT)) == "LLM digest" and len(fake.calls) == 1
    assert "Still waiting" in fake.calls[0]["messages"][0]["content"]


@pytest.mark.functional
def test_morning_pulse_uses_pack_as_cached_system(conn, monkeypatch):
    reset_tenant(conn)
    seed_pack(conn, "# Identity\nUnitOne builds Sentinel.\n\n# Open signals\n- UNI-158 unassigned high")
    fake = FakeAnthropic(text="l1\nl2\nl3\nl4\n\nThree things that need you\n1.\n2.\n3.")
    monkeypatch.setattr(llm, "_client_factory", lambda: fake)
    text = morning_pulse.run(build_ctx(conn, TENANT))
    assert text.startswith("l1")
    sysblock = fake.calls[0]["system"][0]
    assert sysblock["cache_control"] == {"type": "ephemeral"} and sysblock["text"].startswith("# Identity")
    assert "Skill instructions" in sysblock["text"]
    assert fake.calls[0]["messages"] == [{"role": "user", "content": morning_pulse.USER_PROMPT}]
    run = conn.execute("SELECT outcome, tier FROM runs WHERE tenant_id = %s", (TENANT,)).fetchone()
    assert run["outcome"] == text and run["tier"] == 2


@pytest.mark.functional
def test_nudge_stale_drafts_slack_approval(conn, monkeypatch):
    reset_tenant(conn)
    fake = FakeAnthropic(text="Hey Sam, quick check on UNI-170 — any blocker I can clear?")
    monkeypatch.setattr(llm, "_client_factory", lambda: fake)
    seed_issue(conn, "UNI-170", "Fix the thing", assignee="Sam", status="In Progress", updated_days_ago=9)
    seed_signal(conn, "build.stale_in_progress", "UNI-170", severity="medium", kind="open")
    out = nudge_stale.run(build_ctx(conn, TENANT))
    assert len(out) == 1 and out[0].id == "nudge-uni-170" and out[0].type == "Slack · nudge"
    assert out[0].exec.server == "Slack" and out[0].exec.tool == "post_message"
    assert out[0].exec.input["text"].startswith("Hey Sam") and out[0].exec.input["channel"]
    assert (
        out[0].created_by_run
        and conn.execute("SELECT tier FROM runs WHERE id = %s", (out[0].created_by_run,)).fetchone()["tier"] == 2
    )
    assert nudge_stale.run(build_ctx(conn, TENANT)) == [] and len(fake.calls) == 1


@pytest.mark.functional
def test_ask_answer_retrieves_and_cites(conn, monkeypatch):
    reset_tenant(conn)
    conn.execute(
        "INSERT INTO brain_docs (tenant_id, path, slice, content) VALUES (%s, 'customers.md', 'customers', %s)",
        (TENANT, "# Customers\nJCI: we told them multi-repo support ships in Q4 behind a flag."),
    )
    conn.execute(
        "INSERT INTO messages (tenant_id, id, source, channel, author, text, occurred_at) VALUES (%s, '1.1', 'slack', '#sales', 'kamal', %s, now())",
        (TENANT, "JCI asked about multi-repo again; we said Q4."),
    )
    hits = ask.retrieve(conn, TENANT, "what did we tell JCI about multi-repo")
    assert {h["ref"] for h in hits} == {"brain:customers.md", "slack:#sales/1.1"}

    fake = FakeAnthropic(text="We told JCI multi-repo ships in Q4 behind a flag. [1]")
    monkeypatch.setattr(llm, "_client_factory", lambda: fake)
    text = ask.answer(build_ctx(conn, TENANT), "what did we tell JCI about multi-repo")
    assert text.startswith("We told JCI") and "Sources:" in text and "brain:customers.md" in text
    assert "[1] brain:customers.md" in fake.calls[0]["messages"][0]["content"]
    run = conn.execute("SELECT trigger, skill FROM runs WHERE tenant_id = %s", (TENANT,)).fetchone()
    assert run["trigger"] == "ask" and run["skill"] == "ask.answer"

    # gateway routes free text to ask.answer and commands to approvals
    reply = gateway.handle_text(conn, TENANT, "what did we tell JCI about multi-repo")
    assert reply.startswith("We told JCI")
    assert gateway.handle_text(conn, TENANT, "pending") == "Nothing waiting for you."
    assert "No approval" in gateway.handle_text(conn, TENANT, "approve nope-1")


def test_parse_aors_skips_front_matter_and_reads_arrow_heuristics():
    from daemon.skills.build.assign_owner import parse_aors, pick_owner

    doc = (
        "---\nslice: team\nupdated: 2026-09-04\n---\n# Team\n"
        "| Person | Role | Owns |\n|---|---|---|\n| Alexey | Fixer / eval | The fixer + eval path (graph). |\n"
        "## Assignment heuristics\n- Fixer, eval, corpus, graph, skills → Alexey.\n- Customer-labelled issues (customer:*) → Manmeet.\n"
    )
    aors = parse_aors(doc)
    names = [n for n, _ in aors]
    assert "updated" not in names and "Alexey" in names and "Manmeet" in names
    owner, hits = pick_owner("workspace-graph build is 24 min per parent; corpus scale infeasible", aors)
    assert owner == "Alexey" and set(hits) >= {"graph", "corpus"}
