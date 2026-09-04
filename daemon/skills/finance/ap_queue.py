"""finance.ap_queue — Tier 0. Bills due within 7 days and not cleared → record-only approvals with Brex deep links.

StartupOS never moves money: the founder clicks Pay inside Brex. Approving here only records the decision.
"""

from __future__ import annotations

from datetime import timedelta

from common.ids import approval_id
from common.models import Approval
from daemon import approvals, llm
from daemon.skills.base import Ctx, Skill, approval_exists, register, signal_exists

NAME = "finance.ap_queue"
BREX_BILLS_URL = "https://dashboard.brex.com/bills"
NOT_OPEN_PAYMENT = ("CLEARED", "SETTLED", "PAID")
NOT_OPEN_STATUS = ("CANCELED", "VOID", "SETTLED", "PAID")


def due_bills(ctx: Ctx) -> list[dict]:
    conn, tenant, now = ctx["conn"], ctx["tenant_id"], ctx["now"]
    rows = conn.execute(
        """SELECT * FROM bills
           WHERE tenant_id = %s AND due_at IS NOT NULL AND due_at <= %s
             AND coalesce(upper(payment_status), '') <> ALL(%s)
             AND coalesce(upper(status), '') <> ALL(%s)
           ORDER BY due_at""",
        (tenant, now + timedelta(days=7), list(NOT_OPEN_PAYMENT), list(NOT_OPEN_STATUS)),
    ).fetchall()
    return [dict(r) for r in rows]


def run(ctx: Ctx) -> list[Approval]:
    conn, tenant, now = ctx["conn"], ctx["tenant_id"], ctx["now"]
    bills = due_bills(ctx)
    run_id = llm.record_tier0(conn, tenant, NAME, "signal", f"{len(bills)} bills due within 7d")
    proposed: list[Approval] = []
    for b in bills:
        aid = approval_id("ap", b["id"])
        if approval_exists(conn, aid):
            continue
        due = b["due_at"].date().isoformat()
        overdue = b["due_at"] < now
        vendor = b.get("vendor_name") or b.get("vendor_id") or "vendor"
        amount = f"{b['amount']} {b.get('currency') or 'USD'}"
        invoice = f" · {b['invoice_number']}" if b.get("invoice_number") else ""
        status_note = "OVERDUE" if overdue else f"due {due}"
        approval = Approval(
            id=aid,
            tenant_id=tenant,
            module="finance",
            type="Brex · pay",
            target=f"{vendor} · {amount} · {status_note}",
            preview=(
                f"{vendor} {amount}{invoice} is {status_note} and not yet cleared "
                f"(payment status: {b.get('payment_status') or 'unknown'}). "
                f"Pay in Brex: {BREX_BILLS_URL} — StartupOS never moves money; approving only records the decision."
            ),
            exec=None,
            created_by_run=run_id,
            signal_id=signal_exists(conn, f"finance.bill_due_7d:{b['id']}"),
        )
        proposed.append(approvals.propose(conn, approval))
    return proposed


SKILL = register(
    Skill(
        name=NAME,
        module="finance",
        tier=0,
        trigger="signal",
        run=run,
        description="Bills due in 7 days → record-only approvals with Brex deep links",
    )
)
