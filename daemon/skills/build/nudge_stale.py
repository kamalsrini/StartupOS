"""build.nudge_stale — stale In-Progress issue → Tier 2 drafted Slack nudge → Approval exec Slack.post_message.

Tier 0 detection lives in the signal engine (`build.stale_in_progress`); this skill only drafts.
One model call per stale issue, capped per run (batching guard from Architecture Brief §3.4). Skips when the
budget refuses (conserve/exhausted) or no model key exists — a nudge is not worth a Tier-0 template.
"""

from __future__ import annotations

from typing import Any

from common.ids import approval_id
from common.models import Approval, Exec
from daemon import approvals, llm
from daemon.skills.base import Ctx, Skill, approval_exists, open_signals, register, signal_exists, system_prompt

NAME = "build.nudge_stale"
MAX_PER_RUN = 5

INSTRUCTIONS = """
You draft short, kind Slack nudges from the founder to the engineer who owns a stale In-Progress issue.
Two sentences max, first person, name the issue id and ask for a status or a blocker. No emoji, no signature.
"""


def _issue(ctx: Ctx, issue_id: str) -> dict[str, Any]:
    row = (
        ctx["conn"]
        .execute("SELECT * FROM issues WHERE tenant_id = %s AND id = %s", (ctx["tenant_id"], issue_id))
        .fetchone()
    )
    return dict(row) if row else {}


def run(ctx: Ctx) -> list[Approval]:
    conn, tenant = ctx["conn"], ctx["tenant_id"]
    channel = ctx["settings"].slack_digest_channel or "#eng"
    proposed: list[Approval] = []
    drafted = 0
    for sig in open_signals(conn, tenant, "build.stale_in_progress"):
        if drafted >= MAX_PER_RUN:
            break
        issue_id = sig.get("entity_id") or sig["id"].split(":", 1)[-1]
        aid = approval_id("nudge", issue_id)
        if approval_exists(conn, aid):
            continue
        issue = _issue(ctx, issue_id)
        owner = issue.get("assignee") or "the owner"
        title = issue.get("title") or sig.get("title") or issue_id
        updated = issue.get("updated_at")
        prompt = (
            f'Issue {issue_id} "{title}" is In Progress, owned by {owner}, last updated {updated}. Draft the nudge.'
        )
        try:
            res = llm.call(
                2,
                NAME,
                system_prompt(ctx, INSTRUCTIONS),
                [{"role": "user", "content": prompt}],
                trigger="signal",
                max_tokens=200,
                high_priority=False,
                conn=conn,
                tenant_id=tenant,
            )
        except (llm.BudgetExhausted, llm.LLMUnavailable):
            break  # nothing to propose without a draft; the signal stays open for the next cycle
        drafted += 1
        text = res.text.strip()
        approval = Approval(
            id=aid,
            tenant_id=tenant,
            module="build",
            type="Slack · nudge",
            target=f"{issue_id} → {owner} · {channel}",
            preview=text,
            exec=Exec(server="Slack", tool="post_message", input={"channel": channel, "text": text}),
            created_by_run=res.run_id,
            signal_id=signal_exists(conn, sig["id"]),
        )
        proposed.append(approvals.propose(conn, approval))
    return proposed


SKILL = register(
    Skill(
        name=NAME,
        module="build",
        tier=2,
        trigger="signal",
        run=run,
        description="Stale In-Progress issue → drafted Slack nudge for approval",
    )
)
