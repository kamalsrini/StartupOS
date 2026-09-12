"""cockpit.chief_of_staff — the hub agent with a fake model: signals written/resolved, proposals gated by the
allow-list, the brief stored as the run outcome, invalid JSON → retry → degraded Tier-0 fallback."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from track_b_fakes import TENANT, FakeAnthropic, reset_tenant, seed_bill, seed_issue, seed_pack, seed_signal

from daemon import llm, scheduler, skills
from daemon.skills import build_ctx
from daemon.skills.cockpit import chief_of_staff as cos
from daemon.skills.cockpit import morning_pulse

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

GOOD = {
    "brief": "Cash covers b1 (12,500.00 due 2026-09-07) but nothing after; ACM-158 is unassigned high.",
    "risks": [
        {
            "horizon_days": 3,
            "module": "finance",
            "title": "Paying b1 leaves 2,017.26 USD",
            "why": "WILL: bill b1 12500.00 due 2026-09-07 vs available",
            "severity": "high",
            "proposal_id": None,
        },
        {
            "horizon_days": 7,
            "module": "Build",
            "title": "UNI-158 unassigned high for 3 days",
            "why": "MIGHT: build.unassigned_high:UNI-158",
            "severity": "MEDIUM",
            "proposal_id": None,
        },
        {"horizon_days": 1, "module": "mars", "title": "Bad module goes to cockpit", "why": "x", "severity": "weird"},
    ],
    "asks": [
        {"title": "Decide pay or defer b1", "owner": "Kamal", "by": "2026-09-06", "why": "bill b1 due 2026-09-07"}
    ],
    "proposals": [
        {
            "module": "build",
            "type": "Linear · create",
            "target": "UNI-158 → Alexey",
            "preview": "Assign UNI-158 to Alexey, due 2026-09-11",
            "exec": {"server": "Linear", "tool": "save_issue", "input": {"id": "UNI-158", "assignee": "Alexey"}},
            "signal_id": "build.unassigned_high:UNI-158",
        },
        {
            "module": "finance",
            "type": "Brex · pay",
            "target": "b1",
            "preview": "Pay b1 now",
            "exec": {"server": "Brex", "tool": "pay_bill", "input": {"id": "b1"}},
            "signal_id": None,
        },
        {
            "module": "build",
            "type": "Slack · post",
            "target": "#eng",
            "preview": "Post the deploy freeze note",
            "exec": {"server": "Slack", "tool": "delete_message", "input": {"channel": "C1", "ts": "1"}},
        },
        {
            "module": "finance",
            "type": "Decision",
            "target": "Defer b1 to 2026-09-20",
            "preview": "Record the decision to defer b1 until the JCI invoice lands",
            "exec": None,
            "signal_id": "does-not-exist",
        },
    ],
    "changes_since_last": ["first brief"],
}


class SeqAnthropic(FakeAnthropic):
    """FakeAnthropic that answers a different text per call."""

    def __init__(self, texts: list[str]):
        super().__init__()
        self.texts = list(texts)
        self.messages = SimpleNamespace(create=self._create_seq)

    def _create_seq(self, **kwargs: Any) -> Any:
        self.text = self.texts.pop(0) if self.texts else self.text
        return self._create(**kwargs)


def _seed(conn):
    reset_tenant(conn)
    for sql in ("DELETE FROM events WHERE tenant_id = %s", "DELETE FROM connections WHERE tenant_id = %s"):
        conn.execute(sql, (TENANT,))
    seed_pack(conn, "# Identity\nUnitOne builds Sentinel.\n")
    seed_issue(conn, "UNI-158", "workspace-graph build is 24 min per parent", labels=["Bug"], updated_days_ago=3)
    seed_signal(conn, "build.unassigned_high", "UNI-158")
    seed_bill(conn, "b1", "Contractor One", "12500.00", due_in_days=3)
    seed_signal(conn, "finance.bill_due_7d", "b1", module="finance", kind="reply")
    conn.execute(
        "INSERT INTO events (tenant_id, source, entity, entity_id, kind, diff, occurred_at) VALUES (%s,'linear','issues','UNI-158','updated','{}', now() - interval '1 day')",
        (TENANT,),
    )
    conn.execute(
        "INSERT INTO connections (id, tenant_id, source, secret_ref, status) VALUES ('c_vercel', %s, 'vercel', 'env:VERCEL_TOKEN', 'needs_reauth') ON CONFLICT (tenant_id, source) DO UPDATE SET status = 'needs_reauth'",
        (TENANT,),
    )


def test_registered_as_hub_skill_and_scheduled_before_the_pulse():
    s = skills.get("cockpit.chief_of_staff")
    assert s.module == "cockpit" and s.tier == 2 and s.high_priority is True and s.trigger == "schedule"
    assert skills.SCHEDULED_SKILLS[0] == "cockpit.chief_of_staff"
    assert cos.PROMPT_PATH.exists() and "STRICT JSON" in cos.prompt_text()


def test_scheduler_job_runs_half_an_hour_before_the_pulse(monkeypatch):
    monkeypatch.setattr(
        scheduler, "tenant_cadence", lambda t: {"timezone": "UTC", "pulse_hour": 9, "pulse_channel": "web"}
    )
    jobs = scheduler.job_table(TENANT)
    assert [j["id"] for j in jobs][:2] == ["chief_of_staff", "morning_pulse"]
    assert jobs[0]["cron"] == {"hour": 8, "minute": 30} and jobs[1]["cron"] == {"hour": 9, "minute": 0}
    monkeypatch.setattr(
        scheduler, "tenant_cadence", lambda t: {"timezone": "UTC", "pulse_hour": 0, "pulse_channel": "web"}
    )
    assert scheduler.job_table(TENANT)[0]["cron"] == {"hour": 6, "minute": 30}


def test_parse_output_is_strict_but_tolerates_fences():
    assert cos.parse_output('```json\n{"brief": "ok", "risks": []}\n```')["brief"] == "ok"
    for bad in ("", "not json", '{"risks": []}', '{"brief": "x", "risks": "nope"}', "[1, 2]"):
        with pytest.raises(cos.BadOutput):
            cos.parse_output(bad)


def test_validate_exec_allow_list():
    assert cos.validate_exec(None) is None
    ok = cos.validate_exec({"server": "Slack", "tool": "post_message", "input": {"channel": "C1", "text": "hi"}})
    assert ok.server == "Slack" and ok.tool == "post_message"
    for bad in (
        {"server": "Brex", "tool": "pay_bill", "input": {}},
        {"server": "Linear", "tool": "delete_issue", "input": {}},
        {"server": "Linear", "tool": "save_issue", "input": "x"},
        "Linear.save_issue",
    ):
        with pytest.raises(cos.BadOutput):
            cos.validate_exec(bad)


@pytest.mark.functional
def test_run_writes_signals_proposals_and_brief(conn, monkeypatch):
    _seed(conn)
    fake = FakeAnthropic(text=json.dumps(GOOD))
    monkeypatch.setattr(llm, "_client_factory", lambda: fake)

    out = cos.run(build_ctx(conn, TENANT, now=NOW))
    assert out["status"] == "ok" and out["brief"] == GOOD["brief"]

    # one call: prompt + pack as the cacheable system block, inputs as user JSON
    assert len(fake.calls) == 1
    sysblock = fake.calls[0]["system"][0]
    assert sysblock["cache_control"] == {"type": "ephemeral"} and sysblock["text"].startswith("# Identity")
    assert "# Chief of Staff" in sysblock["text"]
    user = fake.calls[0]["messages"][0]["content"]
    payload = json.loads(user.split("\n\n", 1)[1])
    assert set(payload) >= {
        "open_signals_by_module",
        "events_last_7d_by_entity",
        "lookahead",
        "prior_briefs",
        "prior_risks",
        "decisions_tail",
    }
    assert payload["open_signals_by_module"]["build"][0]["id"] == "build.unassigned_high:UNI-158"
    assert payload["events_last_7d_by_entity"]["issues"]["counts"] == {"updated": 1}
    assert payload["lookahead"]["cash"]["bills_due"][0]["id"] == "b1"
    assert payload["lookahead"]["sources_disconnected"] == [
        {"source": "vercel", "status": "needs_reauth", "last_error": None}
    ]
    assert payload["prior_briefs"] == []

    # risks + asks → cos.risk signals, module = the spoke (normalized), severity normalized
    rows = {
        r["id"]: dict(r)
        for r in conn.execute(
            "SELECT * FROM signals WHERE tenant_id = %s AND rule_id = 'cos.risk' AND resolved_at IS NULL", (TENANT,)
        ).fetchall()
    }
    assert set(rows) == {
        "cos.risk:finance-paying-b1-leaves-2-017-26-usd",
        "cos.risk:build-uni-158-unassigned-high-for-3-days",
        "cos.risk:cockpit-bad-module-goes-to-cockpit",
        "cos.risk:ask-decide-pay-or-defer-b1",
    }
    fin = rows["cos.risk:finance-paying-b1-leaves-2-017-26-usd"]
    assert fin["module"] == "finance" and fin["severity"] == "high" and fin["kind"] == "open"
    assert fin["suggested_skill"] == "cockpit.chief_of_staff" and fin["meta"].startswith("3d · WILL")
    b = rows["cos.risk:build-uni-158-unassigned-high-for-3-days"]
    assert b["module"] == "build" and b["severity"] == "medium"
    assert rows["cos.risk:cockpit-bad-module-goes-to-cockpit"]["severity"] == "medium"
    assert "owner Kamal · by 2026-09-06" in rows["cos.risk:ask-decide-pay-or-defer-b1"]["meta"]
    assert out["risks"][0]["signal_id"] == "cos.risk:finance-paying-b1-leaves-2-017-26-usd"

    # proposals: allow-listed Linear + record-only kept; Brex and Slack.delete_message dropped
    aps = {a["id"]: dict(a) for a in conn.execute("SELECT * FROM approvals WHERE tenant_id = %s", (TENANT,)).fetchall()}
    assert len(aps) == 2 and sorted(out["approvals"]) == sorted(aps)
    lin = next(a for a in aps.values() if a["module"] == "build")
    assert lin["exec"] == {"server": "Linear", "tool": "save_issue", "input": {"id": "UNI-158", "assignee": "Alexey"}}
    assert lin["signal_id"] == "build.unassigned_high:UNI-158" and lin["status"] == "pending"
    dec = next(a for a in aps.values() if a["module"] == "finance")
    assert dec["exec"] is None and dec["type"] == "Decision" and dec["signal_id"] is None
    assert all(a["created_by_run"] == out["run_id"] for a in aps.values())

    # the brief is the run outcome (what /cockpit shows), tier 2, status ok
    run = conn.execute("SELECT * FROM runs WHERE id = %s", (out["run_id"],)).fetchone()
    assert run["skill"] == "cockpit.chief_of_staff" and run["tier"] == 2 and run["status"] == "ok"
    assert run["outcome"] == GOOD["brief"] and run["trigger"] == "schedule"

    # the pulse reads today's brief into its user message
    pulse_fake = FakeAnthropic(text="l1\nl2\nl3\nl4")
    monkeypatch.setattr(llm, "_client_factory", lambda: pulse_fake)
    morning_pulse.run(build_ctx(conn, TENANT, now=NOW + timedelta(hours=1)))
    msg = pulse_fake.calls[0]["messages"][0]["content"]
    assert msg.startswith("Chief of Staff brief") and GOOD["brief"] in msg and msg.endswith(morning_pulse.USER_PROMPT)


@pytest.mark.functional
def test_second_run_resolves_absent_risks_and_feeds_prior_briefs(conn, monkeypatch):
    _seed(conn)
    monkeypatch.setattr(llm, "_client_factory", lambda: FakeAnthropic(text=json.dumps(GOOD)))
    first = cos.run(build_ctx(conn, TENANT, now=NOW))

    second_out = {
        "brief": "Day two: b1 still due; UNI-158 now assigned.",
        "risks": [GOOD["risks"][0]],
        "asks": [],
        "proposals": [],
        "changes_since_last": ["UNI-158 assigned"],
    }
    fake = FakeAnthropic(text=json.dumps(second_out))
    monkeypatch.setattr(llm, "_client_factory", lambda: fake)
    later = NOW + timedelta(days=1)
    out = cos.run(build_ctx(conn, TENANT, now=later, trigger="ask", question="what should I worry about"))
    assert out["signals"]["written"] == ["cos.risk:finance-paying-b1-leaves-2-017-26-usd"]
    assert out["signals"]["resolved"] == 3

    payload = json.loads(fake.calls[0]["messages"][0]["content"].split("\n\n", 1)[1])
    assert payload["trigger"] == "ask" and payload["question"] == "what should I worry about"
    assert [b["brief"] for b in payload["prior_briefs"]] == [GOOD["brief"]]
    assert {r["id"] for r in payload["prior_risks"]["open"]} == {
        "cos.risk:finance-paying-b1-leaves-2-017-26-usd",
        "cos.risk:build-uni-158-unassigned-high-for-3-days",
        "cos.risk:cockpit-bad-module-goes-to-cockpit",
        "cos.risk:ask-decide-pay-or-defer-b1",
    }
    open_ids = {
        r["id"]
        for r in conn.execute(
            "SELECT id FROM signals WHERE tenant_id = %s AND rule_id = 'cos.risk' AND resolved_at IS NULL", (TENANT,)
        )
    }
    assert open_ids == {"cos.risk:finance-paying-b1-leaves-2-017-26-usd"}
    resolved = conn.execute(
        "SELECT resolved_at FROM signals WHERE id = 'cos.risk:build-uni-158-unassigned-high-for-3-days'"
    ).fetchone()
    assert resolved["resolved_at"] == later
    run = conn.execute("SELECT trigger, outcome FROM runs WHERE id = %s", (out["run_id"],)).fetchone()
    assert run["trigger"] == "ask" and run["outcome"] == second_out["brief"] and out["run_id"] != first["run_id"]


@pytest.mark.functional
def test_invalid_json_retries_once_then_degrades_to_tier0(conn, monkeypatch):
    _seed(conn)
    fake = SeqAnthropic(["Sure! Here is my analysis: cash is fine.", "still not json {"])
    monkeypatch.setattr(llm, "_client_factory", lambda: fake)
    out = cos.run(build_ctx(conn, TENANT, now=NOW))

    assert len(fake.calls) == 2
    retry_msgs = fake.calls[1]["messages"]
    assert retry_msgs[1]["role"] == "assistant" and retry_msgs[2]["content"] == cos.RETRY_NUDGE
    assert out["status"] == "degraded" and out["degraded"] is True and out["proposals"] == []
    assert (
        out["brief"].startswith("Chief of Staff (Tier 0") and "b1" in out["brief"] and "No data: vercel" in out["brief"]
    )
    assert out["risks"] and out["risks"][0]["module"] == "finance"
    runs = conn.execute(
        "SELECT tier, status, outcome FROM runs WHERE tenant_id = %s AND skill = 'cockpit.chief_of_staff' ORDER BY started_at, tier DESC",
        (TENANT,),
    ).fetchall()
    assert [(r["tier"], r["status"]) for r in runs] == [(2, "ok"), (2, "degraded"), (0, "degraded")]
    assert runs[-1]["outcome"] == out["brief"]
    # fallback risks still reach the spokes
    sig = conn.execute(
        "SELECT module, severity FROM signals WHERE tenant_id = %s AND rule_id = 'cos.risk' AND resolved_at IS NULL ORDER BY id",
        (TENANT,),
    ).fetchall()
    assert sig and {s["module"] for s in sig} >= {"finance"}
    assert conn.execute("SELECT count(*) AS n FROM approvals WHERE tenant_id = %s", (TENANT,)).fetchone()["n"] == 0


@pytest.mark.functional
def test_budget_exhausted_falls_back_without_calling_the_model(conn, monkeypatch):
    _seed(conn)
    fake = FakeAnthropic(text=json.dumps(GOOD))
    monkeypatch.setattr(llm, "_client_factory", lambda: fake)
    from daemon import budget

    b = budget.get_or_create(conn, TENANT)
    conn.execute(
        "UPDATE budgets SET tier2_tokens_used = tier2_tokens_allowed, state = 'exhausted' WHERE tenant_id = %s AND month = %s",
        (TENANT, b["month"]),
    )
    out = cos.run(build_ctx(conn, TENANT, now=NOW))
    assert fake.calls == [] and out["status"] == "degraded" and out["brief"].startswith("Chief of Staff (Tier 0")
    # conserve mode: high_priority → still runs
    conn.execute(
        "UPDATE budgets SET tier2_tokens_used = tier2_tokens_allowed * 0.95, state = 'conserve' WHERE tenant_id = %s AND month = %s",
        (TENANT, b["month"]),
    )
    out = cos.run(build_ctx(conn, TENANT, now=NOW))
    assert len(fake.calls) == 1 and out["status"] == "ok"


@pytest.mark.functional
def test_once_path_via_scheduler_run_skill(conn, monkeypatch):
    """`python -m daemon.main --once cockpit.chief_of_staff` goes through scheduler.run_skill; nothing extra needed."""
    from contextlib import contextmanager

    _seed(conn)
    monkeypatch.setattr(llm, "_client_factory", lambda: FakeAnthropic(text=json.dumps(GOOD)))

    @contextmanager
    def fake_conn(*_a, **_k):
        yield conn

    monkeypatch.setattr(scheduler, "get_conn", fake_conn)
    out = scheduler.run_skill("cockpit.chief_of_staff", TENANT, now=NOW)
    assert out["brief"] == GOOD["brief"] and len(out["approvals"]) == 2
