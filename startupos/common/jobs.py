"""Onboarding job chain — the queue side (Sprint 3a, Track O; CONTRACTS.md "Track O").

`tenant_jobs` is the durable hand-off between the API (which never runs ingest or a model) and the daemon:

    POST /onboarding/compile  →  enqueue_chain(conn, tenant_id)   (this module; tenant-bound API connection)
    daemon/jobs.py service_jobs() every 15 s → tenant_jobs_claim() → execute → done | queued (retry) | failed

Kinds, in chain order: `backfill:<source>` for each connected source ingest knows how to sync, then `signals`,
`context_pack`, `chief_of_staff`, `morning_pulse`. Enqueueing is idempotent: a kind with a queued or running job for
the tenant is not duplicated (the existing row is returned in its place). `status_summary()` is what
GET /onboarding/status reports so the wizard can show the chain instead of "compiling…".
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

BACKFILL_PREFIX = "backfill:"
SIGNALS = "signals"
CONTEXT_PACK = "context_pack"
CHIEF_OF_STAFF = "chief_of_staff"
MORNING_PULSE = "morning_pulse"
TAIL_KINDS: tuple[str, ...] = (SIGNALS, CONTEXT_PACK, CHIEF_OF_STAFF, MORNING_PULSE)
# Sources with an ingest module today (ingest.runner.SOURCES). Kept here so the API never imports ingest/.
BACKFILL_SOURCES: tuple[str, ...] = ("linear", "slack", "brex", "vercel")
ACTIVE: tuple[str, ...] = ("queued", "running")
JOB_COLUMNS = "id, tenant_id, kind, payload, status, attempts, error, run_after, created_at, started_at, finished_at"


def backfill_kind(source: str) -> str:
    return f"{BACKFILL_PREFIX}{source}"


def backfill_source(kind: str) -> str | None:
    """'backfill:linear' → 'linear'; None for the other kinds."""
    return kind[len(BACKFILL_PREFIX) :] if kind.startswith(BACKFILL_PREFIX) else None


def connected_sources(conn: psycopg.Connection, tenant_id: str) -> list[str]:
    """Sources the tenant has connected (status <> 'disabled') that ingest can backfill, in BACKFILL_SOURCES order."""
    rows = conn.execute(
        "SELECT source FROM connections WHERE tenant_id = %s AND status <> 'disabled'", (tenant_id,)
    ).fetchall()
    have = {r["source"] for r in rows}
    return [s for s in BACKFILL_SOURCES if s in have]


def chain_kinds(conn: psycopg.Connection, tenant_id: str) -> list[str]:
    """The ordered kinds POST /onboarding/compile enqueues for this tenant right now."""
    return [backfill_kind(s) for s in connected_sources(conn, tenant_id)] + list(TAIL_KINDS)


def enqueue(conn: psycopg.Connection, tenant_id: str, kind: str, payload: dict[str, Any] | None = None) -> dict:
    """Queue one job unless a queued/running one of the same kind exists for the tenant (idempotent)."""
    existing = conn.execute(
        f"SELECT {JOB_COLUMNS} FROM tenant_jobs WHERE tenant_id = %s AND kind = %s AND status = ANY(%s) "
        "ORDER BY created_at, id LIMIT 1",
        (tenant_id, kind, list(ACTIVE)),
    ).fetchone()
    if existing:
        return {**dict(existing), "enqueued": False}
    row = conn.execute(
        f"INSERT INTO tenant_jobs (tenant_id, kind, payload) VALUES (%s, %s, %s) RETURNING {JOB_COLUMNS}",
        (tenant_id, kind, Jsonb(payload or {})),
    ).fetchone()
    return {**dict(row), "enqueued": True}


def enqueue_chain(conn: psycopg.Connection, tenant_id: str) -> list[dict]:
    """backfill:<source> per connected source → signals → context_pack → chief_of_staff → morning_pulse.

    Rows are inserted in this order inside the caller's transaction, so `created_at` ties and `id` breaks them — the
    claim function orders by both. Returns one entry per kind (new or the already-active row).
    """
    out: list[dict] = []
    for kind in chain_kinds(conn, tenant_id):
        source = backfill_source(kind)
        out.append(enqueue(conn, tenant_id, kind, {"source": source} if source else {}))
    return out


def public(job: dict) -> dict[str, Any]:
    """The API shape of a job row: no payload internals beyond `source`/`result`, times as ISO strings."""
    payload = job.get("payload") or {}
    return {
        "id": job["id"],
        "kind": job["kind"],
        "status": job["status"],
        "attempts": job["attempts"],
        "error": job.get("error"),
        "source": payload.get("source"),
        "result": payload.get("result"),
        "created_at": _iso(job.get("created_at")),
        "started_at": _iso(job.get("started_at")),
        "finished_at": _iso(job.get("finished_at")),
    }


def status_summary(conn: psycopg.Connection, tenant_id: str) -> dict[str, Any]:
    """{queued, running, done, failed, last_error, chain: [...]} for GET /onboarding/status.

    `chain` is the latest row per kind (the current compile's view), oldest first, so the wizard can draw progress.
    """
    counts = {k: 0 for k in ("queued", "running", "done", "failed")}
    for r in conn.execute(
        "SELECT status, count(*) AS n FROM tenant_jobs WHERE tenant_id = %s GROUP BY status", (tenant_id,)
    ).fetchall():
        counts[r["status"]] = int(r["n"])
    last_error = conn.execute(
        "SELECT error FROM tenant_jobs WHERE tenant_id = %s AND error IS NOT NULL ORDER BY finished_at DESC NULLS LAST, id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    chain = conn.execute(
        f"""SELECT DISTINCT ON (kind) {JOB_COLUMNS} FROM tenant_jobs WHERE tenant_id = %s
            ORDER BY kind, created_at DESC, id DESC""",
        (tenant_id,),
    ).fetchall()
    ordered = sorted((dict(r) for r in chain), key=lambda r: (r["created_at"], r["id"]))
    return {**counts, "last_error": last_error["error"] if last_error else None, "chain": [public(r) for r in ordered]}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
