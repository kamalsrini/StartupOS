"""daemon.llm — cost math (pure) and the gateway contract against a fake client (needs Postgres)."""

from __future__ import annotations

import pytest
from track_b_fakes import TENANT, FakeAnthropic, reset_tenant

from daemon import budget, llm

# --- pure ---------------------------------------------------------------------------------------------


def test_cost_tier2_matches_pricing_table():
    # 10k uncached in, 8k cached, 1k out at 3.00 / 0.30 / 15.00 per million
    assert llm.cost(2, 10_000, 8_000, 1_000) == pytest.approx(0.03 + 0.0024 + 0.015)


def test_cost_tier1_is_cheaper_and_cached_is_a_tenth():
    assert llm.cost(1, 1_000_000, 0, 0) == pytest.approx(1.00)
    assert llm.cost(1, 0, 1_000_000, 0) == pytest.approx(0.10)
    assert llm.cost(1, 0, 0, 1_000_000) == pytest.approx(5.00)
    assert llm.cost(2, 0, 0, 0) == 0.0


def test_worked_budget_morning_pulse_is_about_three_cents():
    # Architecture Brief §3.3: morning pulse 10k in (8k cached), 1k out ≈ $0.03
    assert llm.cost(2, 2_000, 8_000, 1_000) == pytest.approx(0.0234, abs=0.001)


def test_tier0_has_no_model():
    with pytest.raises(ValueError):
        llm.model_for(0)


def test_only_llm_imports_anthropic():
    import subprocess  # noqa: S404
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    hits = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            (
                "import pathlib,re,sys;root=pathlib.Path(sys.argv[1]);"
                "print('\\n'.join(str(p.relative_to(root)) for p in root.rglob('*.py') "
                "if 'tests' not in p.parts and '.venv' not in p.parts and re.search(r'^\\s*(import|from)\\s+anthropic', p.read_text(), re.M)))"
            ),
            str(root),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert hits == ["daemon/llm.py"]


# --- with Postgres ------------------------------------------------------------------------------------

pytestmark_db = pytest.mark.functional


@pytest.fixture()
def fake_client(monkeypatch):
    fake = FakeAnthropic(text="Pulse line 1\nline 2\nline 3\nline 4\n\nThree things that need you\n1. a\n2. b\n3. c")
    monkeypatch.setattr(llm, "_client_factory", lambda: fake)
    return fake


@pytest.mark.functional
def test_call_writes_runs_row_and_charges_budget(conn, fake_client):
    reset_tenant(conn)
    res = llm.call(
        2,
        "cockpit.morning_pulse",
        "PACK + instructions",
        [{"role": "user", "content": "go"}],
        trigger="schedule",
        max_tokens=300,
        conn=conn,
        tenant_id=TENANT,
    )

    assert res.text.startswith("Pulse line 1")
    assert res.tier == 2 and res.model == llm.model_for(2)
    assert (res.tokens_in, res.tokens_cached, res.tokens_out) == (1_000, 8_000, 200)
    assert res.cost_usd == pytest.approx(llm.cost(2, 1_000, 8_000, 200))

    # the model call carried a cacheable system block
    kw = fake_client.calls[0]
    assert kw["model"] == llm.model_for(2) and kw["max_tokens"] == 300
    assert kw["system"] == [{"type": "text", "text": "PACK + instructions", "cache_control": {"type": "ephemeral"}}]
    assert kw["messages"] == [{"role": "user", "content": "go"}]

    run = conn.execute("SELECT * FROM runs WHERE id = %s", (res.run_id,)).fetchone()
    assert run["tenant_id"] == TENANT and run["skill"] == "cockpit.morning_pulse" and run["trigger"] == "schedule"
    assert run["tier"] == 2 and run["model"] == res.model and run["status"] == "ok"
    assert (run["tokens_in"], run["tokens_cached"], run["tokens_out"]) == (1_000, 8_000, 200)
    assert float(run["cost_usd"]) == pytest.approx(res.cost_usd, abs=1e-5)
    assert run["outcome"].startswith("Pulse line 1") and run["finished_at"] is not None

    b = budget.get_or_create(conn, TENANT)
    assert b["tier2_tokens_used"] == 9_200 and b["tier1_tokens_used"] == 0
    assert float(b["cost_usd"]) == pytest.approx(res.cost_usd, abs=1e-4)
    assert b["tier2_tokens_allowed"] == budget.ALLOWED_BY_TIER["founder"] and b["state"] == "normal"


@pytest.mark.functional
def test_tier1_call_meters_separately(conn, fake_client):
    reset_tenant(conn)
    res = llm.call(
        1, "brain.pack", "sys", [{"role": "user", "content": "x"}], trigger="schedule", conn=conn, tenant_id=TENANT
    )
    assert res.model == llm.model_for(1)
    b = budget.get_or_create(conn, TENANT)
    assert b["tier1_tokens_used"] == 9_200 and b["tier2_tokens_used"] == 0


@pytest.mark.functional
def test_conserve_blocks_non_priority_and_allows_high_priority(conn, fake_client):
    reset_tenant(conn)
    b = budget.get_or_create(conn, TENANT)
    conn.execute(
        "UPDATE budgets SET tier2_tokens_used = %s, state = 'conserve' WHERE tenant_id = %s AND month = %s",
        (int(b["tier2_tokens_allowed"] * 0.95), TENANT, b["month"]),
    )

    with pytest.raises(llm.BudgetConserve):
        llm.call(
            2,
            "build.nudge_stale",
            "s",
            [{"role": "user", "content": "x"}],
            trigger="signal",
            conn=conn,
            tenant_id=TENANT,
        )
    assert fake_client.calls == []
    refused = conn.execute(
        "SELECT * FROM runs WHERE tenant_id = %s AND skill = 'build.nudge_stale'", (TENANT,)
    ).fetchone()
    assert refused["status"] == "degraded" and refused["tokens_out"] == 0 and float(refused["cost_usd"]) == 0

    res = llm.call(
        2,
        "cockpit.morning_pulse",
        "s",
        [{"role": "user", "content": "x"}],
        trigger="schedule",
        high_priority=True,
        conn=conn,
        tenant_id=TENANT,
    )
    assert res.text and len(fake_client.calls) == 1


@pytest.mark.functional
def test_exhausted_refuses_even_high_priority(conn, fake_client):
    reset_tenant(conn)
    b = budget.get_or_create(conn, TENANT)
    conn.execute(
        "UPDATE budgets SET tier2_tokens_used = tier2_tokens_allowed, state = 'exhausted' WHERE tenant_id = %s AND month = %s",
        (TENANT, b["month"]),
    )
    with pytest.raises(llm.BudgetExhausted):
        llm.call(
            2,
            "ask.answer",
            "s",
            [{"role": "user", "content": "x"}],
            trigger="ask",
            high_priority=True,
            conn=conn,
            tenant_id=TENANT,
        )
    assert fake_client.calls == []
    # Tier 1 still runs when Tier 2 is exhausted (the OS keeps running on Tier 0/1)
    assert llm.call(1, "x", "s", [{"role": "user", "content": "x"}], trigger="ask", conn=conn, tenant_id=TENANT).text


@pytest.mark.functional
def test_api_error_records_error_run(conn, monkeypatch):
    reset_tenant(conn)

    class Boom:
        class messages:  # noqa: N801
            @staticmethod
            def create(**_):
                raise RuntimeError("upstream 529 overloaded")

    monkeypatch.setattr(llm, "_client_factory", lambda: Boom())
    with pytest.raises(RuntimeError):
        llm.call(2, "x", "s", [{"role": "user", "content": "x"}], trigger="ask", conn=conn, tenant_id=TENANT)
    run = conn.execute("SELECT * FROM runs WHERE tenant_id = %s", (TENANT,)).fetchone()
    assert run["status"] == "error" and "overloaded" in run["outcome"]


@pytest.mark.functional
def test_no_api_key_raises_unavailable(conn, monkeypatch):
    reset_tenant(conn)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_client_factory", llm._default_client_factory)
    with pytest.raises(llm.LLMUnavailable):
        llm.call(2, "x", "s", [{"role": "user", "content": "x"}], trigger="ask", conn=conn, tenant_id=TENANT)
    run = conn.execute("SELECT * FROM runs WHERE tenant_id = %s", (TENANT,)).fetchone()
    assert run["status"] == "degraded"


@pytest.mark.functional
def test_record_tier0(conn):
    reset_tenant(conn)
    rid = llm.record_tier0(conn, TENANT, "finance.ap_queue", "signal", "3 bills due")
    run = conn.execute("SELECT * FROM runs WHERE id = %s", (rid,)).fetchone()
    assert run["tier"] == 0 and run["model"] is None and float(run["cost_usd"]) == 0 and run["outcome"] == "3 bills due"


def test_usage_counts_cache_creation_tokens_at_premium():
    from daemon.llm import _usage

    class U:
        input_tokens = 18
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 4000
        output_tokens = 325

    class R:
        usage = U()

    tokens_in, cached, out = _usage(R())
    assert tokens_in == 18 + 5000 and cached == 0 and out == 325
