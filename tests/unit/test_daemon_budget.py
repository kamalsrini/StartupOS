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
