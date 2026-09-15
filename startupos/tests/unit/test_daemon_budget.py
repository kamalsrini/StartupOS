"""daemon.budget — state transitions (pure) and charge() against Postgres."""

from __future__ import annotations

from datetime import date

import pytest
from track_b_fakes import TENANT, reset_tenant

from daemon import budget


def test_state_transitions():
    allowed = 1_000
    assert budget.state(0, allowed) == "normal"
    assert budget.state(899, allowed) == "normal"
    assert budget.state(900, allowed) == "conserve"
    assert budget.state(999, allowed) == "conserve"
    assert budget.state(1_000, allowed) == "exhausted"
    assert budget.state(5_000, allowed) == "exhausted"
    assert budget.state(0, 0) == "exhausted"


def test_allowances_by_tier():
    assert budget.ALLOWED_BY_TIER == {"founder": 1_500_000, "team": 5_000_000, "growth": 20_000_000}


def test_month_start():
    from datetime import UTC, datetime

    assert budget.month_start(datetime(2026, 9, 4, 12, tzinfo=UTC)) == date(2026, 9, 1)


@pytest.mark.functional
def test_get_or_create_uses_tenant_tier(conn):
    reset_tenant(conn)
    conn.execute("UPDATE tenants SET tier = 'team' WHERE id = %s", (TENANT,))
    try:
        row = budget.get_or_create(conn, TENANT, date(2026, 9, 1))
        assert row["tier2_tokens_allowed"] == 5_000_000 and row["state"] == "normal"
        again = budget.get_or_create(conn, TENANT, date(2026, 9, 1))
        assert again == row
    finally:
        conn.execute("UPDATE tenants SET tier = 'founder' WHERE id = %s", (TENANT,))


@pytest.mark.functional
def test_charge_walks_through_states(conn):
    reset_tenant(conn)
    month = date(2026, 9, 1)
    budget.get_or_create(conn, TENANT, month, allowed=1_000)
    row = budget.charge(conn, TENANT, 2, 500, 0.01, month)
    assert row["state"] == "normal" and row["tier2_tokens_used"] == 500
    row = budget.charge(conn, TENANT, 2, 400, 0.01, month)
    assert row["state"] == "conserve"
    row = budget.charge(conn, TENANT, 1, 10_000, 0.05, month)  # tier 1 never moves the tier-2 state
    assert row["state"] == "conserve" and row["tier1_tokens_used"] == 10_000
    row = budget.charge(conn, TENANT, 2, 100, 0.01, month)
    assert row["state"] == "exhausted" and float(row["cost_usd"]) == pytest.approx(0.08)
    assert budget.current_state(conn, TENANT, month) == "exhausted"


# --- self-serve tenants (Sprint 3d PE review, fix 1) -------------------------------------------------------
#
# Self-serve sign-up means a stranger with the public URL arrives with a tenant the daemon spends tokens for.
# Rate limiting caps how fast they arrive; the tier is what caps what each one can then spend. The tier is the
# ONLY input to an allowance, so a trial tenant cannot drift out of step with its budget and an operator
# promotes one with a single UPDATE.


def test_the_self_serve_tier_is_small_and_configurable(monkeypatch):
    monkeypatch.delenv("STARTUPOS_SIGNUP_TIER2_TOKENS", raising=False)
    assert budget.signup_allowance() == budget.DEFAULT_SIGNUP_TIER2_TOKENS == 100_000
    assert budget.signup_allowance() < budget.ALLOWED_BY_TIER["founder"]
    assert budget.allowed_for_tier(budget.SELF_SERVE_TIER) == 100_000
    # An operator's tiers are untouched by any of this.
    assert budget.allowed_for_tier("founder") == 1_500_000
    assert budget.allowed_for_tier("growth") == 20_000_000
    assert budget.allowed_for_tier(None) == budget.DEFAULT_ALLOWED
    assert budget.allowed_for_tier("nonsense") == budget.DEFAULT_ALLOWED

    monkeypatch.setenv("STARTUPOS_SIGNUP_TIER2_TOKENS", "25000")
    assert budget.allowed_for_tier(budget.SELF_SERVE_TIER) == 25_000  # read per call, not at import
    monkeypatch.setenv("STARTUPOS_SIGNUP_TIER2_TOKENS", "")
    assert budget.signup_allowance() == budget.DEFAULT_SIGNUP_TIER2_TOKENS  # empty is unset, not zero
    monkeypatch.setenv("STARTUPOS_SIGNUP_TIER2_TOKENS", "lots")
    assert budget.signup_allowance() == budget.DEFAULT_SIGNUP_TIER2_TOKENS  # a typo is never a bigger budget
    monkeypatch.setenv("STARTUPOS_SIGNUP_TIER2_TOKENS", "-5")
    assert budget.signup_allowance() == 0  # and 0 is 'exhausted', which is the safe direction


@pytest.mark.functional
def test_a_self_serve_tenant_is_budgeted_from_its_tier(conn, monkeypatch):
    reset_tenant(conn)
    monkeypatch.setenv("STARTUPOS_SIGNUP_TIER2_TOKENS", "60000")
    conn.execute("UPDATE tenants SET tier = %s WHERE id = %s", (budget.SELF_SERVE_TIER, TENANT))
    try:
        assert budget.allowed_for(conn, TENANT) == 60_000
        row = budget.get_or_create(conn, TENANT, date(2026, 9, 1))
        assert row["tier2_tokens_allowed"] == 60_000 and row["state"] == "normal"
    finally:
        conn.execute("DELETE FROM budgets WHERE tenant_id = %s AND month = %s", (TENANT, date(2026, 9, 1)))
        conn.execute("UPDATE tenants SET tier = 'founder' WHERE id = %s", (TENANT,))


@pytest.mark.functional
def test_the_degradation_ladder_still_walks_at_the_smaller_number(conn, monkeypatch):
    """normal → conserve → exhausted is a ratio, so a smaller allowance must degrade the same way — and the
    tenant must land in 'exhausted' at its own number, not at the founder one it never had."""
    reset_tenant(conn)
    monkeypatch.setenv("STARTUPOS_SIGNUP_TIER2_TOKENS", "100000")
    month = date(2026, 9, 1)
    conn.execute("UPDATE tenants SET tier = %s WHERE id = %s", (budget.SELF_SERVE_TIER, TENANT))
    try:
        assert budget.get_or_create(conn, TENANT, month)["tier2_tokens_allowed"] == 100_000
        assert budget.charge(conn, TENANT, 2, 80_000, 0.5, month)["state"] == "normal"
        assert budget.charge(conn, TENANT, 2, 9_999, 0.1, month)["state"] == "normal"  # 89 999 / 100 000
        assert budget.charge(conn, TENANT, 2, 1, 0.0, month)["state"] == "conserve"  # exactly 90%
        assert budget.charge(conn, TENANT, 2, 10_000, 0.1, month)["state"] == "exhausted"
        assert budget.current_state(conn, TENANT, month) == "exhausted"
        # Tier 1 keeps the OS alive and never moves the Tier-2 state, at this allowance as at any other.
        assert budget.charge(conn, TENANT, 1, 50_000, 0.2, month)["state"] == "exhausted"
    finally:
        conn.execute("DELETE FROM budgets WHERE tenant_id = %s AND month = %s", (TENANT, month))
        conn.execute("UPDATE tenants SET tier = 'founder' WHERE id = %s", (TENANT,))
