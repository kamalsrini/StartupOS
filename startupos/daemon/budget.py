"""Per-tenant monthly token budgets (Architecture Brief §3.4).

State machine on Tier-2 tokens used vs allowed:
    normal  (< 90%)  → conserve (>= 90%: only high_priority Tier-2 skills run)
                     → exhausted (>= 100%: Tier 2 refused, Tier 0/1 keep the OS alive)

Tier-1 tokens are metered (tier1_tokens_used, cost_usd) but do not count against the Tier-2 allowance.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import psycopg

# Monthly Tier-2 token allowance by tenant tier (ASSUMPTIONS TO VERIFY against pricing page).
ALLOWED_BY_TIER: dict[str, int] = {
    "founder": 1_500_000,
    "team": 5_000_000,
    "growth": 20_000_000,
}
DEFAULT_ALLOWED = ALLOWED_BY_TIER["founder"]

# Self-serve sign-up (Sprint 3d): a tenant nobody vetted, created by a stranger who had the public URL. It is a
# real tenant in every other way, so it gets a real tier — just a small one. `tenants.tier` is the only thing
# that decides an allowance, so a trial tenant cannot drift out of step with its budget, and an operator
# promotes one with a single UPDATE (`tier = 'founder'`) instead of editing a budget row per month, forever.
SELF_SERVE_TIER = "self_serve"
DEFAULT_SIGNUP_TIER2_TOKENS = (
    100_000  # ~7% of the founder tier: enough to see the product work, not to be worth abusing
)


def signup_allowance() -> int:
    """Monthly Tier-2 allowance for a self-serve tenant — STARTUPOS_SIGNUP_TIER2_TOKENS.

    Read per call (not at import) so an operator can change it without a rebuild and tests can set it. A
    missing, empty or unparseable value is the conservative default; a negative one clamps to 0, which
    `state()` already treats as exhausted.
    """
    raw = os.environ.get("STARTUPOS_SIGNUP_TIER2_TOKENS")
    if raw in (None, ""):
        return DEFAULT_SIGNUP_TIER2_TOKENS
    try:
        return max(0, int(str(raw).strip()))
    except ValueError:
        return DEFAULT_SIGNUP_TIER2_TOKENS


def allowed_for_tier(tier: str | None) -> int:
    """The monthly Tier-2 allowance a tier carries. Unknown tiers fall back to the founder allowance."""
    if tier == SELF_SERVE_TIER:
        return signup_allowance()
    return ALLOWED_BY_TIER.get(tier or "founder", DEFAULT_ALLOWED)


CONSERVE_AT = 0.90
EXHAUSTED_AT = 1.00


def month_start(now: datetime | date | None = None) -> date:
    now = now or datetime.now(UTC)
    return date(now.year, now.month, 1)


def state(used: int, allowed: int) -> str:
    """Pure transition function. allowed <= 0 is treated as exhausted (nothing may run)."""
    if allowed <= 0:
        return "exhausted"
    ratio = used / allowed
    if ratio >= EXHAUSTED_AT:
        return "exhausted"
    if ratio >= CONSERVE_AT:
        return "conserve"
    return "normal"


def allowed_for(conn: psycopg.Connection, tenant_id: str) -> int:
    row = conn.execute("SELECT tier FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
    tier = (row or {}).get("tier") or "founder"
    return allowed_for_tier(tier)


def get_or_create(
    conn: psycopg.Connection, tenant_id: str, month: date | None = None, allowed: int | None = None
) -> dict[str, Any]:
    """Return the budgets row for tenant-month, creating it from the tenant's tier when missing."""
    month = month or month_start()
    row = conn.execute("SELECT * FROM budgets WHERE tenant_id = %s AND month = %s", (tenant_id, month)).fetchone()
    if row:
        return dict(row)
    allowed = allowed if allowed is not None else allowed_for(conn, tenant_id)
    row = conn.execute(
        """INSERT INTO budgets (tenant_id, month, tier2_tokens_allowed, state)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (tenant_id, month) DO UPDATE SET tenant_id = EXCLUDED.tenant_id
           RETURNING *""",
        (tenant_id, month, allowed, state(0, allowed)),
    ).fetchone()
    return dict(row)


def charge(
    conn: psycopg.Connection,
    tenant_id: str,
    tier: int,
    tokens: int,
    cost: float | Decimal,
    month: date | None = None,
) -> dict[str, Any]:
    """Add tokens/cost for one call and recompute state. Returns the updated row."""
    month = month or month_start()
    get_or_create(conn, tenant_id, month)
    col = "tier2_tokens_used" if tier == 2 else "tier1_tokens_used"
    row = conn.execute(
        f"""UPDATE budgets SET {col} = {col} + %s, cost_usd = cost_usd + %s
            WHERE tenant_id = %s AND month = %s RETURNING *""",  # noqa: S608 - col is a constant chosen above
        (int(tokens), Decimal(str(round(float(cost), 4))), tenant_id, month),
    ).fetchone()
    new_state = state(row["tier2_tokens_used"], row["tier2_tokens_allowed"])
    if new_state != row["state"]:
        row = conn.execute(
            "UPDATE budgets SET state = %s WHERE tenant_id = %s AND month = %s RETURNING *",
            (new_state, tenant_id, month),
        ).fetchone()
    return dict(row)


def current_state(conn: psycopg.Connection, tenant_id: str, month: date | None = None) -> str:
    row = get_or_create(conn, tenant_id, month)
    return state(row["tier2_tokens_used"], row["tier2_tokens_allowed"])
