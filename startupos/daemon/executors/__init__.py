"""Executors: carry out APPROVED approvals only, through a hard allow-list.

    run_approved(conn, approval_id) -> approvals row (dict)

Rules (Architecture Brief §2.1, CONTRACTS "Approval"):
  * the row is re-read (FOR UPDATE) immediately before executing; anything not `approved` is refused,
  * only Linear.save_issue and Slack.post_message may run — everything else (Brex especially) is marked
    `failed` with code `not_allowed` and nothing is called,
  * the executor acts with the approval's tenant's OWN credential (its `connections` row → secret_ref, resolved on
    the tenant-bound connection: common.secrets.credential_for_source). No credential → `failed`/`executor_error`;
    there is no fallback to the operator's environment (PE review, Sprint 3a: a second tenant's approved action
    used to run against the operator's Linear/Slack),
  * the outcome is written back as `executed` {text, url} or `failed` {error, code}, plus a Tier-0 `runs` row.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import psycopg

from common import secrets
from daemon import llm
from daemon.executors import linear, slack

log = logging.getLogger("daemon.executors")

# (input, the acting tenant's credential for that server's source) → {text, url}
Executor = Callable[[dict[str, Any], str | None], dict[str, Any]]

ALLOW_LIST: dict[tuple[str, str], Executor] = {
    ("Linear", "save_issue"): lambda inp, key: linear.save_issue(inp, api_key=key),
    ("Slack", "post_message"): lambda inp, key: slack.post_message(inp, token=key),
}
# Which `connections.source` holds the credential an executor server acts with.
SOURCE_OF_SERVER: dict[str, str] = {"Linear": "linear", "Slack": "slack"}


class NotApproved(RuntimeError):
    """Raised when run_approved is asked to execute a row whose status is not `approved`."""


def is_allowed(server: str | None, tool: str | None) -> bool:
    return (server or "", tool or "") in ALLOW_LIST


def _finish(conn: psycopg.Connection, approval_id: str, status: str, result: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute(
        "UPDATE approvals SET status = %s, result = %s::jsonb WHERE id = %s RETURNING *",
        (status, json.dumps(result), approval_id),
    ).fetchone()
    return dict(row)


def run_approved(conn: psycopg.Connection, approval_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM approvals WHERE id = %s FOR UPDATE", (approval_id,)).fetchone()
    if not row:
        raise KeyError(approval_id)
    if row["status"] != "approved":
        raise NotApproved(f"approval {approval_id} is {row['status']!r}; executors run only 'approved' rows")

    exec_spec = row["exec"]
    if isinstance(exec_spec, str):
        exec_spec = json.loads(exec_spec)
    tenant = row["tenant_id"]

    if not exec_spec:  # record-only: the decision was already logged to decisions.md at approve time
        llm.record_tier0(conn, tenant, "exec.record_only", "approval", f"recorded {approval_id}")
        return _finish(conn, approval_id, "executed", {"text": "Recorded (no action taken)", "url": None})

    server, tool = exec_spec.get("server"), exec_spec.get("tool")
    skill = f"exec.{server}.{tool}"
    if not is_allowed(server, tool):
        log.warning("refused executor outside allow-list: %s.%s (approval %s)", server, tool, approval_id)
        llm.record_tier0(conn, tenant, skill, "approval", "refused: not in allow-list", status="error")
        return _finish(
            conn, approval_id, "failed", {"error": f"{server}.{tool} is not an allowed executor", "code": "not_allowed"}
        )

    try:
        credential = secrets.credential_for_source(conn, tenant, SOURCE_OF_SERVER[server])
        result = ALLOW_LIST[(server, tool)](exec_spec.get("input") or {}, credential)
    except Exception as exc:  # executor failure → failed row, never crash the daemon loop
        message = f"{type(exc).__name__}: {str(exc)[:500]}"
        llm.record_tier0(conn, tenant, skill, "approval", f"failed: {message}", status="error")
        return _finish(conn, approval_id, "failed", {"error": message, "code": "executor_error"})

    llm.record_tier0(conn, tenant, skill, "approval", result.get("text"))
    return _finish(conn, approval_id, "executed", {"text": result.get("text"), "url": result.get("url")})


def run_all_approved(conn: psycopg.Connection, tenant_id: str) -> list[dict[str, Any]]:
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM approvals WHERE tenant_id = %s AND status = 'approved' ORDER BY decided_at", (tenant_id,)
        ).fetchall()
    ]
    out = []
    for aid in ids:
        try:
            out.append(run_approved(conn, aid))
        except NotApproved:
            continue
    return out
