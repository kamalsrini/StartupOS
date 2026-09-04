"""Ingest entry point.

    python -m ingest.runner [--source linear|slack|brex|vercel|all] [--fixtures] [--loop]

Live API when the source's secret is present (and --fixtures is not given), else tests/fixtures. Each pass
updates connections.last_sync_at / last_error. --loop re-runs each source on its own interval.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from common.db import ensure_tenant, get_conn
from common.settings import settings
from ingest import brex, linear, slack, vercel
from ingest.base import UpsertResult

log = logging.getLogger("ingest")

SOURCES: dict[str, ModuleType] = {"linear": linear, "slack": slack, "brex": brex, "vercel": vercel}
INTERVALS = {"linear": 900, "slack": 300, "brex": 3600, "vercel": 3600}
SECRET_REFS = {"linear": linear.KEY_REF, "slack": slack.KEY_REF, "brex": brex.KEY_REF, "vercel": vercel.KEY_REF}


def _connection_config(source: str) -> dict[str, Any]:
    if source == "slack":
        return {"channels": list(settings.slack_channels)}
    if source == "vercel" and settings.vercel_team_id:
        return {"team_id": settings.vercel_team_id}
    return {}


def upsert_connection(
    conn: psycopg.Connection,
    tenant_id: str,
    source: str,
    *,
    mode: str,
    error: str | None,
    when: datetime | None = None,
) -> None:
    """Create/update the connections row. Credentials never touch this table — secret_ref only."""
    now = when or datetime.now(UTC)
    config = {**_connection_config(source), "mode": mode}
    conn.execute(
        """INSERT INTO connections (id, tenant_id, source, secret_ref, config, status, last_sync_at, last_error)
           VALUES (%s, %s, %s, %s, %s, 'connected', %s, %s)
           ON CONFLICT (tenant_id, source) DO UPDATE SET
             config = EXCLUDED.config,
             last_sync_at = CASE WHEN EXCLUDED.last_error IS NULL THEN EXCLUDED.last_sync_at ELSE connections.last_sync_at END,
             last_error = EXCLUDED.last_error""",
        (
            f"{tenant_id}-{source}",
            tenant_id,
            source,
            SECRET_REFS[source],
            Jsonb(config),
            None if error else now,
            error,
        ),
    )


def run_source(
    conn: psycopg.Connection, tenant_id: str, source: str, *, force_fixtures: bool = False
) -> dict[str, UpsertResult]:
    mod = SOURCES[source]
    use_fixtures = force_fixtures or not mod.has_credentials()
    mode = "fixtures" if use_fixtures else "live"
    try:
        results = mod.sync(conn, tenant_id, use_fixtures=use_fixtures)
    except Exception as exc:  # record the failure on the connection, never the secret
        conn.rollback()
        upsert_connection(conn, tenant_id, source, mode=mode, error=f"{type(exc).__name__}: {exc}"[:500])
        conn.commit()
        log.error("ingest %s failed: %s", source, type(exc).__name__)
        raise
    upsert_connection(conn, tenant_id, source, mode=mode, error=None)
    conn.commit()
    for table, res in results.items():
        log.info("ingest %s/%s (%s): +%d ~%d =%d", source, table, mode, res.created, res.updated, res.unchanged)
    return results


def run_all(
    conn: psycopg.Connection, tenant_id: str, sources: list[str] | None = None, *, force_fixtures: bool = False
) -> dict[str, dict[str, UpsertResult]]:
    out: dict[str, dict[str, UpsertResult]] = {}
    for source in sources or list(SOURCES):
        try:
            out[source] = run_source(conn, tenant_id, source, force_fixtures=force_fixtures)
        except Exception:  # keep the other sources running; error is already on the connections row
            out[source] = {}
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest.runner", description=__doc__)
    parser.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    parser.add_argument("--fixtures", action="store_true", help="force fixture data even if secrets are present")
    parser.add_argument("--loop", action="store_true", help="keep running on per-source intervals")
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    sources = list(SOURCES) if args.source == "all" else [args.source]
    tenant_id = settings.tenant_id

    if not args.loop:
        with get_conn(args.dsn) as conn:
            ensure_tenant(conn, tenant_id)
            results = run_all(conn, tenant_id, sources, force_fixtures=args.fixtures)
        _print_summary(results)
        return 0

    next_run = dict.fromkeys(sources, 0.0)
    while True:
        now = time.monotonic()
        due = [s for s in sources if next_run[s] <= now]
        if due:
            with get_conn(args.dsn) as conn:
                ensure_tenant(conn, tenant_id)
                results = run_all(conn, tenant_id, due, force_fixtures=args.fixtures)
            _print_summary(results)
            for s in due:
                next_run[s] = time.monotonic() + INTERVALS[s]
        sleep_for = max(1.0, min(next_run[s] for s in sources) - time.monotonic())
        time.sleep(sleep_for)


def _print_summary(results: dict[str, dict[str, UpsertResult]]) -> None:
    for source, tables in results.items():
        if not tables:
            print(f"{source}: FAILED (see connections.last_error)")
            continue
        parts = ", ".join(f"{t}: +{r.created} ~{r.updated} ={r.unchanged}" for t, r in tables.items())
        print(f"{source}: {parts}")


if __name__ == "__main__":
    sys.exit(main())
