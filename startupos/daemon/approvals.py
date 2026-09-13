"""The approval gate: propose (pending) → decide (approved|declined) → executors (executed|failed).

Record-only approvals (exec is null) never run anything; on approve the decision is appended to
brain_docs decisions.md so the brain learns from what the founder chose.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import psycopg

from common.models import Approval, Decision

log = logging.getLogger("daemon.approvals")


def _row_to_approval(row: dict[str, Any]) -> Approval:
    return Approval.model_validate(dict(row))


def get(conn: psycopg.Connection, approval_id: str) -> Approval | None:
    row = conn.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,)).fetchone()
    return _row_to_approval(row) if row else None


def announce(conn: psycopg.Connection, approval: Approval) -> dict[str, Any] | None:
    """Post a freshly proposed approval to the tenant's Slack with its Approve/Decline buttons (Sprint 3b, B).

    Best effort in both directions: `deliver_approval` already returns `skipped` rather than raising for a
    web-only tenant, a tenant with no Slack credential or a Slack outage, and anything it does not catch is
    caught here. Proposing an approval must never fail because Slack is unhappy — the cockpit still has it.
    """
    from daemon import delivery  # local: delivery imports the executors, which import this module's siblings

    try:
        return delivery.deliver_approval(conn, approval.tenant_id, approval)
    except Exception as exc:
        log.warning("approval %s proposed but not delivered: %s", approval.id, type(exc).__name__)
        return None


def propose(conn: psycopg.Connection, approval: Approval) -> Approval:
    """Insert a pending approval, and post it to Slack the first time. Idempotent on id: an existing row (any
    status) is returned unchanged and is NOT posted again (the deliveries UNIQUE constraint is a second guard)."""
    exec_json = json.dumps(approval.exec.model_dump()) if approval.exec else None
    inserted = conn.execute(
        """INSERT INTO approvals (id, tenant_id, module, type, target, preview, exec, status, created_by_run, signal_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'pending', %s, %s)
           ON CONFLICT (tenant_id, id) DO NOTHING""",
        (
            approval.id,
            approval.tenant_id,
            approval.module,
            approval.type,
            approval.target,
            approval.preview,
            exec_json,
            approval.created_by_run,
            approval.signal_id,
        ),
    )
    stored = get(conn, approval.id)
    assert stored is not None
    if inserted.rowcount == 1 and stored.status == "pending":
        announce(conn, stored)
    return stored


def list_pending(conn: psycopg.Connection, tenant_id: str, module: str | None = None) -> list[Approval]:
    if module:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE tenant_id = %s AND module = %s AND status = 'pending' ORDER BY created_at",
            (tenant_id, module),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM approvals WHERE tenant_id = %s AND status = 'pending' ORDER BY created_at", (tenant_id,)
        ).fetchall()
    return [_row_to_approval(r) for r in rows]


def list_by_status(conn: psycopg.Connection, tenant_id: str, status: str) -> list[Approval]:
    rows = conn.execute(
        "SELECT * FROM approvals WHERE tenant_id = %s AND status = %s ORDER BY created_at", (tenant_id, status)
    ).fetchall()
    return [_row_to_approval(r) for r in rows]


def record_decision(conn: psycopg.Connection, approval: Approval, decision: Decision) -> None:
    """Append one line to brain_docs decisions.md (insert a minimal doc if the tenant has none)."""
    when = (approval.decided_at or datetime.now(UTC)).date().isoformat()
    verdict = "Approved" if decision.decision == "approve" else "Declined"
    line = f"- {when} · {verdict} · {approval.type} · {approval.target} · by {decision.decided_by}"
    if decision.reason:
        line += f" · reason: {decision.reason.strip()}"
    updated = conn.execute(
        """UPDATE brain_docs
           SET content = content || %s, version = version + 1, source = 'decision', updated_at = now()
           WHERE tenant_id = %s AND path = 'decisions.md'""",
        ("\n" + line, approval.tenant_id),
    )
    if updated.rowcount == 0:
        conn.execute(
            """INSERT INTO brain_docs (tenant_id, path, slice, content, source)
               VALUES (%s, 'decisions.md', 'decisions', %s, 'decision')
               ON CONFLICT (tenant_id, path) DO UPDATE
               SET content = brain_docs.content || %s, version = brain_docs.version + 1, updated_at = now()""",
            (approval.tenant_id, "# Decisions\n\n" + line, "\n" + line),
        )


def decide(conn: psycopg.Connection, approval_id: str, decision: Decision) -> Approval:
    """Move a pending approval to approved/declined. Anything not pending is refused with ValueError."""
    row = conn.execute("SELECT * FROM approvals WHERE id = %s FOR UPDATE", (approval_id,)).fetchone()
    if not row:
        raise KeyError(approval_id)
    if row["status"] != "pending":
        raise ValueError(f"approval {approval_id} is {row['status']}, not pending")

    new_status = "approved" if decision.decision == "approve" else "declined"
    preview = decision.edited_preview if decision.edited_preview else row["preview"]
    decline_reason = decision.reason if decision.decision == "decline" else None
    row = conn.execute(
        """UPDATE approvals
           SET status = %s, preview = %s, decided_by = %s, decided_at = now(), decline_reason = %s
           WHERE id = %s RETURNING *""",
        (new_status, preview, decision.decided_by, decline_reason, approval_id),
    ).fetchone()
    approval = _row_to_approval(row)

    # Record-only approvals: the decision itself is the outcome — log it to the brain.
    if approval.exec is None or decision.decision == "decline":
        record_decision(conn, approval, decision)
    return approval
