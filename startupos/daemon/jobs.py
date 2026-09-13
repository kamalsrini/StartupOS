"""Onboarding job service (Sprint 3a, Track O): drains `tenant_jobs` for every tenant.

`service_jobs()` is ONE scheduler job (every 15 s, process-wide — not per tenant). Each pass:

  1. claims the next runnable job through the SECURITY DEFINER `tenant_jobs_claim()` (db/rls.sql) on an anonymous
     service connection — oldest first, FOR UPDATE SKIP LOCKED, and only when no earlier job of that tenant is still
     queued/running (per-tenant ordering; done/failed rows never block, so the chain continues past a failed backfill);
  2. executes it on a connection bound to its tenant (RLS scopes every read and write to that tenant);
  3. marks it done, or re-queues it with backoff (up to MAX_ATTEMPTS), or fails it — on the same tenant-bound
     connection — and goes back to 1 until nothing is claimable (or `max_jobs` is reached).

Job bodies reuse the runtime the scheduler already has: `ingest.runner.run_tenant` for `backfill:<source>`,
`signals.engine.run`, `brain.pack.compile`, and the `cockpit.chief_of_staff` / `cockpit.morning_pulse` skills (both
fall back to Tier 0 with no ANTHROPIC key or budget — the first pulse needs no model).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from common import jobs as queue
from common.jobs import JOB_COLUMNS, backfill_source
from common.tenants import anonymous_conn, tenant_conn

log = logging.getLogger("daemon.jobs")

MAX_ATTEMPTS = 3
# Seconds to wait before attempt 2 and attempt 3. Tests set this to (0, 0).
BACKOFF_SECONDS: tuple[int, ...] = (15, 60)
SERVICE_JOBS_SECONDS = 15
MAX_JOBS_PER_PASS = 50

Handler = Callable[[psycopg.Connection, str, dict[str, Any]], Any]


class BackfillFailed(RuntimeError):
    """The ingest pass for one source failed; the detail is what ingest wrote to connections.last_error."""


# --- job bodies (every one runs on a tenant-bound connection) ---------------------------------------------


def run_backfill(conn: psycopg.Connection, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from ingest import runner

    source = payload.get("source") or ""
    if source not in runner.SOURCES:
        raise BackfillFailed(f"no ingest module for source {source!r}")
    out = runner.run_tenant(conn, tenant_id, [source])
    if source not in out:
        raise BackfillFailed(f"{source}: no active connection for this tenant")
    result = out[source]
    if result is None:
        row = conn.execute(
            "SELECT last_error FROM connections WHERE tenant_id = %s AND source = %s", (tenant_id, source)
        ).fetchone()
        raise BackfillFailed(f"{source}: {(row or {}).get('last_error') or 'sync failed'}")
    return {t: {"created": r.created, "updated": r.updated, "unchanged": r.unchanged} for t, r in result.items()}


def run_signals(conn: psycopg.Connection, tenant_id: str, _payload: dict[str, Any]) -> dict[str, Any]:
    from signals import engine

    out = engine.run(conn, tenant_id)
    return {k: v for k, v in out.items() if k in ("created", "updated", "resolved", "open")}


def run_context_pack(conn: psycopg.Connection, tenant_id: str, _payload: dict[str, Any]) -> dict[str, Any]:
    from brain import pack

    content = pack.compile(conn, tenant_id)
    return {"chars": len(content), "token_estimate": len(content) // 4}


def _run_skill(name: str) -> Handler:
    def handler(conn: psycopg.Connection, tenant_id: str, _payload: dict[str, Any]) -> dict[str, Any]:
        from daemon import skills
        from daemon.skills import build_ctx

        out = skills.get(name).run(build_ctx(conn, tenant_id))
        if isinstance(out, dict):  # chief_of_staff
            return {
                "status": out.get("status"),
                "run_id": out.get("run_id"),
                "signals": len(out.get("signals") or []),
                "approvals": len(out.get("approvals") or []),
            }
        if isinstance(out, str):  # morning_pulse
            return {"chars": len(out)}
        return {"type": type(out).__name__}

    return handler


HANDLERS: dict[str, Handler] = {
    queue.SIGNALS: run_signals,
    queue.CONTEXT_PACK: run_context_pack,
    queue.CHIEF_OF_STAFF: _run_skill("cockpit.chief_of_staff"),
    queue.MORNING_PULSE: _run_skill("cockpit.morning_pulse"),
}


def handler_for(kind: str) -> Handler:
    if backfill_source(kind) is not None:
        return run_backfill
    try:
        return HANDLERS[kind]
    except KeyError:
        raise KeyError(f"unknown job kind {kind!r}") from None


# --- claim / finish -----------------------------------------------------------------------------------------


def claim_next(conn: psycopg.Connection, tenants: list[str] | None = None) -> dict[str, Any] | None:
    """Claim (and commit) the next runnable job across tenants via tenant_jobs_claim(); None when idle."""
    row = conn.execute("SELECT * FROM tenant_jobs_claim(%s)", (tenants,)).fetchone()
    conn.commit()
    return dict(row) if row else None


def backoff_for(attempts: int) -> int:
    """Seconds before the next attempt after `attempts` failed ones (attempt 1 failed → BACKOFF_SECONDS[0])."""
    if not BACKOFF_SECONDS:
        return 0
    return int(BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS)) - 1])


def finish(conn: psycopg.Connection, job: dict[str, Any], *, error: str | None, result: Any = None) -> str:
    """Record the outcome on the tenant-bound connection: done | queued (retry with backoff) | failed."""
    now = datetime.now(UTC)
    if error is None:
        conn.execute(
            "UPDATE tenant_jobs SET status = 'done', error = NULL, finished_at = %s, payload = payload || %s WHERE id = %s",
            (now, Jsonb({"result": result if result is not None else {}}), job["id"]),
        )
        return "done"
    if job["attempts"] < MAX_ATTEMPTS:
        delay = backoff_for(job["attempts"])
        conn.execute(
            "UPDATE tenant_jobs SET status = 'queued', error = %s, finished_at = %s, run_after = %s WHERE id = %s",
            (error[:2000], now, now + timedelta(seconds=delay), job["id"]),
        )
        return "queued"
    conn.execute(
        "UPDATE tenant_jobs SET status = 'failed', error = %s, finished_at = %s WHERE id = %s",
        (error[:2000], now, job["id"]),
    )
    return "failed"


def execute(job: dict[str, Any], *, dsn: str | None = None) -> str:
    """Run one claimed job on a connection bound to its tenant and record the outcome. Returns the new status."""
    tenant_id, kind = job["tenant_id"], job["kind"]
    payload = dict(job.get("payload") or {})
    error: str | None = None
    result: Any = None
    with tenant_conn(tenant_id, dsn) as conn:
        try:
            result = handler_for(kind)(conn, tenant_id, payload)
            conn.commit()
        except Exception as exc:  # the outcome goes on the row, never up the stack — the queue must keep moving
            conn.rollback()  # the binding survives (get_conn committed it); partial work of this attempt is undone
            error = f"{type(exc).__name__}: {exc}"[:2000]
            log.warning("job %s %s/%s attempt %d failed: %s", job["id"], tenant_id, kind, job["attempts"], error)
        status = finish(conn, job, error=error, result=result)
    log.info("job %s %s/%s → %s", job["id"], tenant_id, kind, status)
    return status


def service_jobs(
    tenants: list[str] | None = None, *, dsn: str | None = None, max_jobs: int = MAX_JOBS_PER_PASS
) -> dict[str, Any]:
    """One pass: claim and execute runnable jobs until idle (or `max_jobs`). The scheduler runs this every 15 s.

    `tenants` narrows the claim (the pinned dev scheduler); None = every tenant. Returns counts by outcome plus the
    jobs touched, so tests and operators can see what a pass did.
    """
    out: dict[str, Any] = {"claimed": 0, "done": 0, "queued": 0, "failed": 0, "jobs": []}
    while out["claimed"] < max_jobs:
        try:
            with anonymous_conn(dsn) as conn:
                job = claim_next(conn, tenants)
        except Exception as exc:  # DB down: log and try again on the next tick
            log.warning("job claim failed: %s", type(exc).__name__)
            break
        if job is None:
            break
        out["claimed"] += 1
        try:
            status = execute(job, dsn=dsn)
        except Exception as exc:  # could not even open the tenant connection; the row stays 'running' until requeued
            log.exception("job %s could not run: %s", job["id"], type(exc).__name__)
            status = "running"
        out[status] = out.get(status, 0) + 1
        out["jobs"].append({"id": job["id"], "tenant_id": job["tenant_id"], "kind": job["kind"], "status": status})
    return out


def drain(tenants: list[str] | None = None, *, dsn: str | None = None, max_passes: int = 100) -> dict[str, Any]:
    """Run service_jobs() until a pass claims nothing (tests, `daemon.main --drain-jobs`). Backoff still applies."""
    total: dict[str, Any] = {"claimed": 0, "done": 0, "queued": 0, "failed": 0, "jobs": [], "passes": 0}
    for _ in range(max_passes):
        one = service_jobs(tenants, dsn=dsn)
        total["passes"] += 1
        for k in ("claimed", "done", "queued", "failed"):
            total[k] += one[k]
        total["jobs"] += one["jobs"]
        if one["claimed"] == 0:
            break
    return total


def pending(conn: psycopg.Connection, tenant_id: str) -> list[dict[str, Any]]:
    """Queued/running rows of one tenant (on a tenant-bound connection), oldest first."""
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT {JOB_COLUMNS} FROM tenant_jobs WHERE tenant_id = %s AND status = ANY(%s) ORDER BY created_at, id",
            (tenant_id, list(queue.ACTIVE)),
        ).fetchall()
    ]
