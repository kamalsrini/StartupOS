"""Approvals: list and decide. Deciding only flips pending → approved|declined; executors (daemon) do the rest."""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import fetch_approvals, get_db, get_tenant, now_utc, row_to_approval
from common.models import Approval, Decision

router = APIRouter(prefix="/approvals", tags=["approvals"])


@router.get("", response_model=list[Approval])
def list_approvals(
    module: str | None = Query(default=None),
    status: str | None = Query(default="pending"),
    conn: psycopg.Connection = Depends(get_db),
    tenant_id: str = Depends(get_tenant),
) -> list[Approval]:
    return fetch_approvals(conn, tenant_id, module=module, status=status or None)


@router.get("/{approval_id}", response_model=Approval)
def get_approval(
    approval_id: str,
    conn: psycopg.Connection = Depends(get_db),
    tenant_id: str = Depends(get_tenant),
) -> Approval:
    row = conn.execute("SELECT * FROM approvals WHERE id = %s AND tenant_id = %s", (approval_id, tenant_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="approval not found")
    return row_to_approval(row)


@router.post("/{approval_id}/decide", response_model=Approval)
def decide(
    approval_id: str,
    body: Decision,
    conn: psycopg.Connection = Depends(get_db),
    tenant_id: str = Depends(get_tenant),
) -> Approval:
    row = conn.execute(
        "SELECT * FROM approvals WHERE id = %s AND tenant_id = %s FOR UPDATE", (approval_id, tenant_id)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="approval not found")
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"approval is {row['status']}, not pending")

    new_status = "approved" if body.decision == "approve" else "declined"
    preview = body.edited_preview if body.edited_preview else row["preview"]
    updated = conn.execute(
        """UPDATE approvals
              SET status = %s, decided_by = %s, decided_at = %s, decline_reason = %s, preview = %s
            WHERE id = %s AND tenant_id = %s AND status = 'pending'
        RETURNING *""",
        (
            new_status,
            body.decided_by,
            now_utc(),
            body.reason if new_status == "declined" else None,
            preview,
            approval_id,
            tenant_id,
        ),
    ).fetchone()
    if not updated:  # raced with another decision
        raise HTTPException(status_code=409, detail="approval was decided concurrently")
    return row_to_approval(updated)
