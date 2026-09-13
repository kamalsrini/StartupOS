"""Ingest entry point.

    python -m ingest.runner [--source linear|slack|brex|vercel|all] [--loop]            # every active tenant
    python -m ingest.runner --tenant [ID] [--fixtures]                                  # one tenant (dev/CLI)

Multi-tenant pass (the default, and what `--loop` repeats on per-source intervals): iterate `active_tenants()`;
for each tenant, every source with a `connections` row whose status <> 'disabled'; resolve the credential with
`settings.secret(connection.secret_ref, conn=…, tenant_id=…)` and call `sync(conn, tenant_id, api_key=…)`.
Source modules never read the environment. A credential equal to the literal string "fixture" makes the source
read tests/fixtures (onboarding acceptance test, Track O). Tenants with no connection for a source are skipped
silently. Per-tenant/per-source errors land in `connections.last_error`; one failure never stops the pass.

Single-tenant pass (`--tenant`, `--fixtures`, or STARTUPOS_DEV=1): the dev/CLI path. `settings.tenant_id` is the
default tenant; sources with no connection row are bootstrapped from the operator env refs (`env:LINEAR_API_KEY`
…) when present, else skipped as status=disabled; `--fixtures` forces sample data.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from common import secrets
from common.db import ensure_tenant, get_conn
from common.settings import settings
from common.tenants import dev_mode, list_active_tenants, tenant_conn
from ingest import brex, fixtures, linear, slack, vercel
from ingest.base import UpsertResult

log = logging.getLogger("ingest")

SOURCES: dict[str, ModuleType] = {"linear": linear, "slack": slack, "brex": brex, "vercel": vercel}
INTERVALS = {"linear": 900, "slack": 300, "brex": 3600, "vercel": 3600}
# Operator-managed env refs: the dev/CLI default when a tenant has no connection row for a source.
SECRET_REFS = {"linear": linear.KEY_REF, "slack": slack.KEY_REF, "brex": brex.KEY_REF, "vercel": vercel.KEY_REF}
# connections.config keys handed to sync() as keyword arguments.
CONFIG_KEYS: dict[str, tuple[str, ...]] = {"slack": ("channels",), "vercel": ("team_id",)}
# A credential equal to this literal makes ingest read tests/fixtures for that source (CONTRACTS.md, Track O).
FIXTURE_CREDENTIAL = "fixture"
FIXTURES_DIR: Path = fixtures.FIXTURES_DIR


class MissingCredential(RuntimeError):
    """The connection's secret_ref resolved to nothing. The message names the ref, never a value."""


def _dev_config(source: str) -> dict[str, Any]:
    """Connection config for the dev/CLI bootstrap (from the operator env). Never used for API-created rows."""
    if source == "slack":
        return {"channels": list(settings.slack_channels)}
    if source == "vercel" and settings.vercel_team_id:
        return {"team_id": settings.vercel_team_id}
    return {}


def load_connection(conn: psycopg.Connection, tenant_id: str, source: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM connections WHERE tenant_id = %s AND source = %s", (tenant_id, source)).fetchone()
    return dict(row) if row else None


def active_connections(conn: psycopg.Connection, tenant_id: str, sources: list[str] | None = None) -> list[dict]:
    """The tenant's non-disabled connections for known sources, in SOURCES order."""
    rows = conn.execute(
        "SELECT * FROM connections WHERE tenant_id = %s AND status <> 'disabled'", (tenant_id,)
    ).fetchall()
    wanted = [s for s in (sources or list(SOURCES)) if s in SOURCES]
    by_source = {r["source"]: dict(r) for r in rows}
    return [by_source[s] for s in wanted if s in by_source]


def upsert_connection(
    conn: psycopg.Connection,
    tenant_id: str,
    source: str,
    *,
    mode: str,
    error: str | None,
    when: datetime | None = None,
    secret_ref: str | None = None,
    config: dict[str, Any] | None = None,
) -> None:
    """Create/update the connections row after a pass. Credentials never touch this table — secret_ref only.

    An existing row keeps its secret_ref and its config keys (only `mode` is refreshed); a failed pass keeps the
    previous status and last_sync_at and records last_error; mode="skipped" (dev path, no credentials) disables.
    """
    now = when or datetime.now(UTC)
    cfg = {**(config if config is not None else _dev_config(source)), "mode": mode}
    status = "disabled" if mode == "skipped" else "connected"
    conn.execute(
        """INSERT INTO connections (id, tenant_id, source, secret_ref, config, status, last_sync_at, last_error)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (tenant_id, source) DO UPDATE SET
             config = connections.config || EXCLUDED.config,
             status = CASE WHEN EXCLUDED.status = 'disabled' OR EXCLUDED.last_error IS NULL THEN EXCLUDED.status ELSE connections.status END,
             last_sync_at = CASE WHEN EXCLUDED.last_error IS NULL THEN EXCLUDED.last_sync_at ELSE connections.last_sync_at END,
             last_error = EXCLUDED.last_error""",
        (
            f"{tenant_id}-{source}",
            tenant_id,
            source,
            secret_ref or SECRET_REFS[source],
            Jsonb(cfg),
            status,
            None if error else now,
            error,
        ),
    )


def resolve_credential(conn: psycopg.Connection, tenant_id: str, connection: dict[str, Any]) -> str | None:
    """`settings.secret(secret_ref, conn=, tenant_id=)` — env: refs from the process, kv: refs from tenant_secrets."""
    ref = connection.get("secret_ref") or ""
    if not ref:
        return None
    return settings.secret(ref, conn=conn, tenant_id=tenant_id)


def run_connection(
    conn: psycopg.Connection, tenant_id: str, connection: dict[str, Any], *, force_fixtures: bool = False
) -> dict[str, UpsertResult]:
    """One (tenant, source) sync on a tenant-bound connection. Records the outcome on the connections row.

    Raises after recording so callers can count the failure; the exception text never carries the credential.
    """
    source = connection["source"]
    mod = SOURCES[source]
    cfg = {k: v for k, v in (connection.get("config") or {}).items() if k != "mode"}
    kwargs = {k: cfg[k] for k in CONFIG_KEYS.get(source, ()) if k in cfg}
    mode = "fixtures" if force_fixtures else "live"
    try:
        api_key: str | None = None
        if not force_fixtures:
            key = resolve_credential(conn, tenant_id, connection)
            if key == FIXTURE_CREDENTIAL:
                mode = "fixtures"
            elif not key:
                raise MissingCredential(f"no credential for {connection.get('secret_ref') or '<no secret_ref>'}")
            else:
                api_key = key
        results = mod.sync(
            conn, tenant_id, api_key=api_key, use_fixtures=(mode == "fixtures"), fixtures_dir=FIXTURES_DIR, **kwargs
        )
    except Exception as exc:  # record the failure on the connection, never the secret
        conn.rollback()
        upsert_connection(
            conn,
            tenant_id,
            source,
            mode=mode,
            error=f"{type(exc).__name__}: {exc}"[:500],
            secret_ref=connection.get("secret_ref"),
            config=cfg,
        )
        conn.commit()
        log.error("ingest %s/%s failed: %s", tenant_id, source, type(exc).__name__)
        raise
    upsert_connection(
        conn, tenant_id, source, mode=mode, error=None, secret_ref=connection.get("secret_ref"), config=cfg
    )
    conn.commit()
    for table, res in results.items():
        log.info(
            "ingest %s/%s/%s (%s): +%d ~%d =%d", tenant_id, source, table, mode, res.created, res.updated, res.unchanged
        )
    return results


# --- single-tenant (dev/CLI) pass ------------------------------------------------------------------


def run_source(
    conn: psycopg.Connection, tenant_id: str, source: str, *, force_fixtures: bool = False
) -> dict[str, UpsertResult]:
    """Dev/CLI semantics for one source: fixtures when forced; the tenant's connection row when it has one; else
    bootstrap a row from the operator env ref, or skip as status=disabled when no credential exists anywhere.

    PE review 2026-09-04 still holds: fixture data is never mixed into a real tenant unless asked for explicitly
    (--fixtures / tests) or the connection's credential is the literal "fixture".
    """
    connection = load_connection(conn, tenant_id, source)
    if force_fixtures:
        row = connection or {"source": source, "secret_ref": SECRET_REFS[source], "config": _dev_config(source)}
        return run_connection(conn, tenant_id, row, force_fixtures=True)
    if connection is not None and connection.get("status") != "disabled":
        ref = connection.get("secret_ref") or ""
        if not (ref.startswith("env:") and not settings.secret(ref)):  # an unset env ref → skip below, not an error
            return run_connection(conn, tenant_id, connection)
    if settings.secret(SECRET_REFS[source]):
        # Bootstrap (or re-enable) the row from the operator env ref; the sync below stamps last_sync_at.
        conn.execute(
            """INSERT INTO connections (id, tenant_id, source, secret_ref, config, status)
               VALUES (%s, %s, %s, %s, %s, 'connected')
               ON CONFLICT (tenant_id, source) DO UPDATE SET
                 secret_ref = EXCLUDED.secret_ref, status = 'connected', last_error = NULL,
                 config = connections.config || EXCLUDED.config""",
            (f"{tenant_id}-{source}", tenant_id, source, SECRET_REFS[source], Jsonb(_dev_config(source))),
        )
        conn.commit()
        return run_connection(conn, tenant_id, load_connection(conn, tenant_id, source) or {"source": source})
    upsert_connection(conn, tenant_id, source, mode="skipped", error="no credentials configured")
    conn.commit()
    log.warning("ingest %s skipped: no credentials (use --fixtures for sample data)", source)
    return {}


def run_all(
    conn: psycopg.Connection, tenant_id: str, sources: list[str] | None = None, *, force_fixtures: bool = False
) -> dict[str, dict[str, UpsertResult]]:
    """One tenant, every requested source. {} = skipped (no credentials), None = failed (see last_error)."""
    out: dict[str, dict[str, UpsertResult]] = {}
    for source in sources or list(SOURCES):
        try:
            out[source] = run_source(conn, tenant_id, source, force_fixtures=force_fixtures)
        except Exception:  # keep the other sources running; the error is already on the connections row
            out[source] = None  # type: ignore[assignment]
    return out


# --- multi-tenant pass -------------------------------------------------------------------------------


def run_tenant(
    conn: psycopg.Connection, tenant_id: str, sources: list[str] | None = None
) -> dict[str, dict[str, UpsertResult]]:
    """Every non-disabled connection of one tenant. Sources without a connection row are absent (skipped)."""
    out: dict[str, dict[str, UpsertResult]] = {}
    for connection in active_connections(conn, tenant_id, sources):
        try:
            out[connection["source"]] = run_connection(conn, tenant_id, connection)
        except Exception:  # recorded on connections.last_error by run_connection
            out[connection["source"]] = None  # type: ignore[assignment]
    return out


def run_tenants(
    sources: list[str] | None = None, *, dsn: str | None = None
) -> dict[str, dict[str, dict[str, UpsertResult]] | None]:
    """One pass over active_tenants() × their connections, each tenant on its own bound connection.

    A tenant whose pass cannot even start (its connection fails) is logged and recorded as None; the loop goes on.
    """
    out: dict[str, Any] = {}
    for tenant in list_active_tenants(dsn):
        tid = tenant["id"]
        try:
            with tenant_conn(tid, dsn) as conn:
                out[tid] = run_tenant(conn, tid, sources)
        except Exception as exc:  # never let one tenant stop the pass
            log.exception("ingest pass for tenant %s failed: %s", tid, type(exc).__name__)
            out[tid] = None
    return out


# --- CLI -----------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingest.runner", description=__doc__)
    parser.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    parser.add_argument(
        "--tenant",
        nargs="?",
        const="",
        default=None,
        metavar="ID",
        help="single-tenant (dev/CLI) pass; ID defaults to TENANT_ID / settings.tenant_id",
    )
    parser.add_argument("--fixtures", action="store_true", help="force fixture data for the dev tenant")
    parser.add_argument("--loop", action="store_true", help="keep running on per-source intervals")
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    sources = list(SOURCES) if args.source == "all" else [args.source]
    single = args.tenant is not None or args.fixtures or dev_mode()
    if not single:
        secrets.check_master_key(log)  # every kv: connection needs it; malformed → RuntimeError, unset → warning
    tenant_id = (args.tenant or settings.tenant_id) if single else None

    def one_pass(due: list[str]) -> None:
        if single:
            with get_conn(args.dsn, tenant_id=tenant_id) as conn:
                ensure_tenant(conn, tenant_id)
                results = run_all(conn, tenant_id, due, force_fixtures=args.fixtures)
            _print_summary(results, tenant_id)
            return
        for tid, results in run_tenants(due, dsn=args.dsn).items():
            if results is None:
                print(f"{tid}: FAILED (pass did not start; see the log)")
            else:
                _print_summary(results, tid)

    if not args.loop:
        one_pass(sources)
        return 0

    next_run = dict.fromkeys(sources, 0.0)
    while True:
        now = time.monotonic()
        due = [s for s in sources if next_run[s] <= now]
        if due:
            try:
                one_pass(due)
            except Exception as exc:  # e.g. the DB is down: log and retry on the next interval
                log.exception("ingest pass failed: %s", type(exc).__name__)
            for s in due:
                next_run[s] = time.monotonic() + INTERVALS[s]
        sleep_for = max(1.0, min(next_run[s] for s in sources) - time.monotonic())
        time.sleep(sleep_for)


def _print_summary(results: dict[str, dict[str, UpsertResult]], tenant_id: str | None = None) -> None:
    prefix = f"{tenant_id}/" if tenant_id else ""
    for source, tables in results.items():
        if tables is None:
            print(f"{prefix}{source}: FAILED (see connections.last_error)")
            continue
        if not tables:
            print(f"{prefix}{source}: skipped (no credentials)")
            continue
        parts = ", ".join(f"{t}: +{r.created} ~{r.updated} ={r.unchanged}" for t, r in tables.items())
        print(f"{prefix}{source}: {parts}")


if __name__ == "__main__":
    sys.exit(main())
