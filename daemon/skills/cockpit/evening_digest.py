"""cockpit.evening_digest — Tier 2 (short), 18:00 local: what the OS did, what you decided, what is waiting.

On BudgetExhausted/BudgetConserve or when no ANTHROPIC key is configured it falls back to a Tier 0 digest
computed with plain SQL over runs/approvals/signals (no import of signals.digest — that's Track A's module).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from daemon import llm
from daemon.skills.base import Ctx, Skill, register, system_prompt

NAME = "cockpit.evening_digest"

INSTRUCTIONS = """
You are StartupOS writing the founder's evening digest for Slack. Using the day summary in the user message
and the context pack above, write three short sections: "Done today", "You decided", "Still waiting".
Plain text, bullets with "•", under 120 words, no preamble.
"""


def day_counts(ctx: Ctx) -> dict[str, Any]:
    conn, tenant, now = ctx["conn"], ctx["tenant_id"], ctx["now"]
    since = now - timedelta(hours=24)
    q = conn.execute

    def one(sql: str, *params: Any) -> Any:
        return q(sql, params).fetchone()

    runs = one(
        """SELECT count(*) AS n, coalesce(sum(cost_usd),0) AS cost,
                  count(*) FILTER (WHERE tier = 2) AS t2, count(*) FILTER (WHERE tier = 1) AS t1
           FROM runs WHERE tenant_id = %s AND started_at >= %s""",
        tenant,
        since,
    )
    approvals = one(
        """SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
                  count(*) FILTER (WHERE decided_at >= %s AND status IN ('approved','executed')) AS approved,
                  count(*) FILTER (WHERE decided_at >= %s AND status = 'declined') AS declined,
                  count(*) FILTER (WHERE status = 'executed' AND decided_at >= %s) AS executed,
                  count(*) FILTER (WHERE status = 'failed' AND decided_at >= %s) AS failed
           FROM approvals WHERE tenant_id = %s""",
        since,
        since,
        since,
        since,
        tenant,
    )
    signals = one(
        """SELECT count(*) FILTER (WHERE resolved_at IS NULL) AS open,
                  count(*) FILTER (WHERE resolved_at IS NULL AND severity = 'high') AS high,
                  count(*) FILTER (WHERE first_seen_at >= %s) AS new_today,
                  count(*) FILTER (WHERE resolved_at >= %s) AS resolved_today
           FROM signals WHERE tenant_id = %s""",
        since,
        since,
        tenant,
    )
    by_module = q(
        """SELECT module, count(*) AS n FROM signals WHERE tenant_id = %s AND resolved_at IS NULL
           GROUP BY module ORDER BY n DESC""",
        (tenant,),
    ).fetchall()
    pending_rows = q(
        """SELECT type, target FROM approvals WHERE tenant_id = %s AND status = 'pending'
           ORDER BY created_at LIMIT 5""",
        (tenant,),
    ).fetchall()
    return {
        "runs": dict(runs),
        "approvals": dict(approvals),
        "signals": dict(signals),
        "by_module": [dict(r) for r in by_module],
        "pending": [dict(r) for r in pending_rows],
    }


def format_tier0(c: dict[str, Any]) -> str:
    r, a, s = c["runs"], c["approvals"], c["signals"]
    lines = [
        "Evening digest",
        "Done today",
        f"• {r['n']} runs ({r['t2']} Tier 2, {r['t1']} Tier 1) · ${float(r['cost']):.2f}",
        f"• {s['new_today']} new signals, {s['resolved_today']} resolved · {a['executed']} actions executed"
        + (f", {a['failed']} failed" if a["failed"] else ""),
        "You decided",
        f"• {a['approved']} approved · {a['declined']} declined",
        "Still waiting",
        f"• {a['pending']} approvals pending · {s['open']} signals open ({s['high']} high)",
    ]
    if c["by_module"]:
        lines.append("• Open by module: " + ", ".join(f"{m['module']} {m['n']}" for m in c["by_module"]))
    for p in c["pending"]:
        lines.append(f"  – {p['type']} · {p['target']}")
    return "\n".join(lines)


def run(ctx: Ctx) -> str:
    counts = day_counts(ctx)
    summary = format_tier0(counts)
    try:
        res = llm.call(
            2,
            NAME,
            system_prompt(ctx, INSTRUCTIONS),
            [{"role": "user", "content": f"Day summary (Tier 0 counts):\n{summary}\n\nWrite the digest."}],
            trigger="schedule",
            max_tokens=400,
            high_priority=False,
            conn=ctx["conn"],
            tenant_id=ctx["tenant_id"],
        )
        return res.text
    except (llm.BudgetExhausted, llm.LLMUnavailable):
        llm.record_tier0(ctx["conn"], ctx["tenant_id"], NAME, "schedule", summary, status="degraded")
        return summary


SKILL = register(
    Skill(
        name=NAME,
        module="cockpit",
        tier=2,
        trigger="schedule",
        run=run,
        description="What happened, what you decided, what is waiting (Tier 0 fallback)",
    )
)
