"""LLM gateway — the ONLY module in StartupOS that imports `anthropic`.

Every model call:
  * picks the model by tier (settings.tier2_model / tier1_model),
  * checks the tenant-month budget before Tier 2 (conserve → only high_priority skills; exhausted → BudgetExhausted),
  * sends the system prompt (context pack + skill instructions) as ONE cacheable block (cache_control ephemeral),
  * writes a `runs` row (tier, model, tokens_in/cached/out, cost_usd, status, outcome),
  * charges `budgets` for the tenant-month.

Tier 0 code never calls the model; it uses `record_tier0` so the ledger still shows what ran.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import anthropic
import psycopg

from common.db import get_conn
from common.ids import run_id as new_run_id
from common.models import LLMResult
from common.settings import settings
from daemon import budget

log = logging.getLogger("daemon.llm")

# --- Pricing (USD per million tokens) -------------------------------------------------------------
# ASSUMPTIONS TO VERIFY against the current Anthropic price sheet (Architecture Brief §3.3:
# "frontier-class ~$3/M in, $15/M out; small-class roughly a third; cached input ~a tenth of list").
PRICING_PER_MILLION: dict[int, dict[str, float]] = {
    2: {"in": 3.00, "cached": 0.30, "out": 15.00},  # frontier (Tier 2)
    1: {"in": 1.00, "cached": 0.10, "out": 5.00},  # small (Tier 1)
}


class BudgetExhausted(RuntimeError):
    """Tier-2 budget is at/over 100%; the skill must fall back to Tier 0 or skip."""


class BudgetConserve(BudgetExhausted):
    """Budget >= 90%: a non-high_priority Tier-2 skill was refused. Handled like exhaustion by callers."""


class LLMUnavailable(RuntimeError):
    """No ANTHROPIC_API_KEY configured (or client could not be built). Skills fall back to Tier 0."""


def cost(tier: int, tokens_in: int, tokens_cached: int, tokens_out: int) -> float:
    """USD cost for one call. tokens_in are UNCACHED input tokens (anthropic reports cache reads separately)."""
    p = PRICING_PER_MILLION[tier]
    usd = (tokens_in * p["in"] + tokens_cached * p["cached"] + tokens_out * p["out"]) / 1_000_000
    return round(usd, 6)


def model_for(tier: int) -> str:
    if tier == 2:
        return settings.tier2_model
    if tier == 1:
        return settings.tier1_model
    raise ValueError(f"tier {tier} has no model (Tier 0 is deterministic)")


def has_api_key() -> bool:
    return bool(settings.secret("env:ANTHROPIC_API_KEY"))


def _default_client_factory() -> Any:
    if not has_api_key():
        raise LLMUnavailable("ANTHROPIC_API_KEY is not configured")
    return anthropic.Anthropic()  # reads the key from the environment; never logged


# Tests replace this with a factory returning a fake client exposing .messages.create(**kwargs).
_client_factory: Callable[[], Any] = _default_client_factory


def _write_run(
    conn: psycopg.Connection,
    *,
    run_id: str,
    tenant_id: str,
    trigger: str,
    skill: str,
    tier: int,
    model: str | None,
    tokens_in: int = 0,
    tokens_cached: int = 0,
    tokens_out: int = 0,
    cost_usd: float = 0.0,
    status: str = "ok",
    outcome: str | None = None,
    started_at: datetime | None = None,
) -> None:
    conn.execute(
        """INSERT INTO runs (id, tenant_id, trigger, skill, tier, model, tokens_in, tokens_cached, tokens_out,
                             cost_usd, status, outcome, started_at, finished_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())""",
        (
            run_id,
            tenant_id,
            trigger,
            skill,
            tier,
            model,
            tokens_in,
            tokens_cached,
            tokens_out,
            round(cost_usd, 5),
            status,
            (outcome or "")[:20_000] or None,
            started_at or datetime.now(UTC),
        ),
    )


def record_tier0(
    conn: psycopg.Connection,
    tenant: str,
    skill: str,
    trigger: str,
    outcome: str | None = None,
    *,
    status: str = "ok",
) -> str:
    """Ledger entry for a deterministic (Tier 0) skill run. Zero tokens, zero cost. Returns the run id."""
    rid = new_run_id()
    _write_run(
        conn,
        run_id=rid,
        tenant_id=tenant,
        trigger=trigger,
        skill=skill,
        tier=0,
        model=None,
        status=status,
        outcome=outcome,
    )
    return rid


def _extract_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def _usage(response: Any) -> tuple[int, int, int]:
    u = getattr(response, "usage", None)

    def g(name: str) -> int:
        if u is None:
            return 0
        v = getattr(u, name, None) if not isinstance(u, dict) else u.get(name)
        return int(v or 0)

    return g("input_tokens"), g("cache_read_input_tokens"), g("output_tokens")


def call(
    tier: int,
    skill: str,
    system: str,
    messages: list[dict[str, Any]],
    *,
    trigger: str,
    max_tokens: int = 1024,
    high_priority: bool = False,
    conn: psycopg.Connection | None = None,
    tenant_id: str | None = None,
    temperature: float | None = None,
) -> LLMResult:
    """Make one model call through the gate. Raises BudgetExhausted/BudgetConserve before spending Tier-2 tokens."""
    if tier not in (1, 2):
        raise ValueError("llm.call is for Tier 1/2 only; Tier 0 uses record_tier0")
    if conn is None:
        with get_conn() as own:
            return call(
                tier,
                skill,
                system,
                messages,
                trigger=trigger,
                max_tokens=max_tokens,
                high_priority=high_priority,
                conn=own,
                tenant_id=tenant_id,
                temperature=temperature,
            )

    tenant = tenant_id or settings.tenant_id
    model = model_for(tier)
    rid = new_run_id()
    started = datetime.now(UTC)

    if tier == 2:
        st = budget.current_state(conn, tenant)
        if st == "exhausted":
            _write_run(
                conn,
                run_id=rid,
                tenant_id=tenant,
                trigger=trigger,
                skill=skill,
                tier=tier,
                model=model,
                status="degraded",
                outcome="refused: budget exhausted",
                started_at=started,
            )
            raise BudgetExhausted(f"tenant {tenant} Tier-2 budget exhausted for this month")
        if st == "conserve" and not high_priority:
            _write_run(
                conn,
                run_id=rid,
                tenant_id=tenant,
                trigger=trigger,
                skill=skill,
                tier=tier,
                model=model,
                status="degraded",
                outcome="refused: budget conserve (skill not high_priority)",
                started_at=started,
            )
            raise BudgetConserve(f"tenant {tenant} in conserve mode; {skill} is not high_priority")

    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": messages,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature

    try:
        client = _client_factory()
        response = client.messages.create(**kwargs)
    except LLMUnavailable:
        _write_run(
            conn,
            run_id=rid,
            tenant_id=tenant,
            trigger=trigger,
            skill=skill,
            tier=tier,
            model=model,
            status="degraded",
            outcome="refused: no model credentials",
            started_at=started,
        )
        raise
    except Exception as exc:  # network / API error: record and re-raise, never log secrets
        _write_run(
            conn,
            run_id=rid,
            tenant_id=tenant,
            trigger=trigger,
            skill=skill,
            tier=tier,
            model=model,
            status="error",
            outcome=f"error: {type(exc).__name__}: {str(exc)[:500]}",
            started_at=started,
        )
        log.warning("llm call failed skill=%s tier=%s err=%s", skill, tier, type(exc).__name__)
        raise

    tokens_in, tokens_cached, tokens_out = _usage(response)
    text = _extract_text(response)
    usd = cost(tier, tokens_in, tokens_cached, tokens_out)
    _write_run(
        conn,
        run_id=rid,
        tenant_id=tenant,
        trigger=trigger,
        skill=skill,
        tier=tier,
        model=model,
        tokens_in=tokens_in,
        tokens_cached=tokens_cached,
        tokens_out=tokens_out,
        cost_usd=usd,
        status="ok",
        outcome=text,
        started_at=started,
    )
    budget.charge(conn, tenant, tier, tokens_in + tokens_cached + tokens_out, usd)
    return LLMResult(
        text=text,
        model=model,
        tier=tier,
        tokens_in=tokens_in,
        tokens_cached=tokens_cached,
        tokens_out=tokens_out,
        cost_usd=usd,
        run_id=rid,
    )
