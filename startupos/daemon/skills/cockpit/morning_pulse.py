"""cockpit.morning_pulse — Tier 2, 07:00 local. Four-line pulse + three things that need you.

System = latest context pack (cached) + instructions. Outcome lands in runs.outcome (GET /cockpit reads it).
"""

from __future__ import annotations

from daemon import llm
from daemon.skills.base import Ctx, Skill, register, system_prompt

NAME = "cockpit.morning_pulse"

INSTRUCTIONS = """
You are StartupOS writing the founder's morning pulse. Use only the context pack above.
Format exactly:
- Four short lines (one sentence each) covering cash/finance, build, customers/GTM, and one risk or win.
- A blank line, then the heading "Three things that need you" followed by exactly three numbered items,
  each naming the concrete object (issue id, vendor, account) and the decision requested.
No preamble, no markdown headers other than the one above, under 180 words.
"""

USER_PROMPT = "Draft a four-line pulse + three things that need you."
COS_SKILL = "cockpit.chief_of_staff"


def latest_cos_brief(ctx: Ctx) -> str | None:
    """Today's Chief of Staff brief (runs.outcome of cockpit.chief_of_staff since local midnight-ish), if any."""
    conn, tenant, now = ctx["conn"], ctx["tenant_id"], ctx["now"]
    row = conn.execute(
        """SELECT outcome FROM runs WHERE tenant_id = %s AND skill = %s AND status IN ('ok', 'degraded')
             AND outcome IS NOT NULL AND outcome NOT LIKE 'refused:%%' AND outcome NOT LIKE 'error:%%'
             AND started_at >= %s
           ORDER BY started_at DESC LIMIT 1""",
        (tenant, COS_SKILL, now.replace(hour=0, minute=0, second=0, microsecond=0)),
    ).fetchone()
    return row["outcome"] if row else None


def user_prompt(ctx: Ctx) -> str:
    brief = latest_cos_brief(ctx)
    if not brief:
        return USER_PROMPT
    return f"Chief of Staff brief (this morning, cross-spoke):\n{brief}\n\n{USER_PROMPT}"


def _fallback(ctx: Ctx) -> str:
    conn, tenant = ctx["conn"], ctx["tenant_id"]
    pending = conn.execute(
        "SELECT count(*) AS n FROM approvals WHERE tenant_id = %s AND status = 'pending'", (tenant,)
    ).fetchone()["n"]
    high = conn.execute(
        "SELECT count(*) AS n FROM signals WHERE tenant_id = %s AND resolved_at IS NULL AND severity = 'high'",
        (tenant,),
    ).fetchone()["n"]
    return f"Morning pulse (Tier 0): {high} high signals open · {pending} approvals waiting."


def run(ctx: Ctx) -> str:
    try:
        res = llm.call(
            2,
            NAME,
            system_prompt(ctx, INSTRUCTIONS),
            [{"role": "user", "content": user_prompt(ctx)}],
            trigger="schedule",
            max_tokens=600,
            high_priority=True,  # the pulse is the product's front door; it survives conserve mode
            conn=ctx["conn"],
            tenant_id=ctx["tenant_id"],
        )
        return res.text
    except (llm.BudgetExhausted, llm.LLMUnavailable):
        text = _fallback(ctx)
        llm.record_tier0(ctx["conn"], ctx["tenant_id"], NAME, "schedule", text, status="degraded")
        return text


SKILL = register(
    Skill(
        name=NAME,
        module="cockpit",
        tier=2,
        trigger="schedule",
        run=run,
        high_priority=True,
        description="Four-line pulse + three things that need you",
    )
)
