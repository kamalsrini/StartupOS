"""customers.prioritize_ask — customer ask untouched 3 days → propose reprioritisation (Tier 0 in v1).

Signal `customers.ask_untouched` → Approval exec Linear.save_issue {id, priority: 2, dueDate: +5d}.
"""

from __future__ import annotations

from common.ids import approval_id
from common.models import Approval, Exec
from daemon import approvals, llm
from daemon.skills.base import Ctx, Skill, approval_exists, open_signals, plus_days, register, signal_exists

NAME = "customers.prioritize_ask"


def run(ctx: Ctx) -> list[Approval]:
    conn, tenant = ctx["conn"], ctx["tenant_id"]
    signals = open_signals(conn, tenant, "customers.ask_untouched")
    if not signals:
        return []
    run_id = llm.record_tier0(conn, tenant, NAME, "signal", f"evaluated {len(signals)} untouched customer asks")
    proposed: list[Approval] = []
    for sig in signals:
        issue_id = sig.get("entity_id") or sig["id"].split(":", 1)[-1]
        aid = approval_id("prioritize", issue_id)
        if approval_exists(conn, aid):
            continue
        issue = conn.execute(
            "SELECT title, assignee, labels, priority FROM issues WHERE tenant_id = %s AND id = %s", (tenant, issue_id)
        ).fetchone()
        title = (issue or {}).get("title") or sig.get("title") or issue_id
        labels = [label for label in ((issue or {}).get("labels") or []) if label]
        account = next((label for label in labels if label.lower().startswith(("account", "customer", "acct"))), None)
        due = plus_days(ctx, 5)
        who = f" for {account}" if account else ""
        approval = Approval(
            id=aid,
            tenant_id=tenant,
            module="customers",
            type="Linear · reprioritize",
            target=f"{issue_id} → High · due {due}",
            preview=(
                f'Raise {issue_id} "{title}"{who} to High (priority 2) with due date {due}. '
                f"Rationale: customer ask untouched for 3+ days ({sig.get('meta') or 'no movement'})."
            ),
            exec=Exec(server="Linear", tool="save_issue", input={"id": issue_id, "priority": 2, "dueDate": due}),
            created_by_run=run_id,
            signal_id=signal_exists(conn, sig["id"]),
        )
        proposed.append(approvals.propose(conn, approval))
    return proposed


SKILL = register(
    Skill(
        name=NAME,
        module="customers",
        tier=0,
        trigger="signal",
        run=run,
        description="Untouched customer ask → reprioritise in Linear",
    )
)
