"""Tenant iteration for the runtime services (Sprint 3a, Track T).

The scheduler and the ingest loop run as `startupos_app` (RLS-bound) and must work on every active tenant without
a restart when one signs up. Two helpers, nothing else:

- `active_tenants(conn)` — every `tenants` row with status = 'active', via the SECURITY DEFINER `tenants_active()`
  (db/rls.sql), so the caller needs no tenant bound and never row-scans across tenants from application code.
- `tenant_conn(tenant_id)` — a connection bound to that tenant on the service DSN (`STARTUPOS_APP_DSN`, else
  `DATABASE_URL`). Every per-tenant unit of work opens its own; nothing is shared across tenants.

`settings.tenant_id` is NOT consulted here: it survives only as the dev/CLI default (`STARTUPOS_DEV=1`, `make`
targets, `--fixtures`) in the entry points, never in the multi-tenant paths.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

from common.db import get_conn
from common.settings import settings

CADENCE_DEFAULTS: dict[str, Any] = {"timezone": "America/Los_Angeles", "pulse_hour": 7, "pulse_channel": "web"}


def service_dsn() -> str:
    """The DSN the services run on: the RLS-bound app role when configured, else the superuser/dev DSN."""
    return settings.app_dsn or settings.database_url


def dev_mode() -> bool:
    """STARTUPOS_DEV=1 — the only place `settings.tenant_id` may stand in for a real tenant resolution."""
    return os.environ.get("STARTUPOS_DEV") == "1"


def active_tenants(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """All tenants with status = 'active', in creation order, with their cadence columns."""
    rows = conn.execute("SELECT * FROM tenants_active()").fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["timezone"] = d.get("timezone") or CADENCE_DEFAULTS["timezone"]
        d["pulse_hour"] = int(
            d.get("pulse_hour") if d.get("pulse_hour") is not None else CADENCE_DEFAULTS["pulse_hour"]
        )
        d["pulse_channel"] = d.get("pulse_channel") or CADENCE_DEFAULTS["pulse_channel"]
        out.append(d)
    return out


def active_tenant_ids(conn: psycopg.Connection) -> list[str]:
    return [t["id"] for t in active_tenants(conn)]


@contextmanager
def tenant_conn(tenant_id: str, dsn: str | None = None) -> Iterator[psycopg.Connection]:
    """A connection bound to `tenant_id` on the service DSN. Commits on clean exit, rolls back on error."""
    if not tenant_id:
        raise ValueError("tenant_conn needs a tenant_id (an unbound connection sees nothing under RLS)")
    with get_conn(dsn or service_dsn(), tenant_id=tenant_id) as conn:
        yield conn


@contextmanager
def anonymous_conn(dsn: str | None = None) -> Iterator[psycopg.Connection]:
    """A connection bound to no tenant (app.tenant_id = ''): sees no tenant rows, only SECURITY DEFINER lookups."""
    with get_conn(dsn or service_dsn(), tenant_id="") as conn:
        yield conn


def list_active_tenants(dsn: str | None = None) -> list[dict[str, Any]]:
    """`active_tenants` on a fresh anonymous connection — what the scheduler and ingest loop call every pass."""
    with anonymous_conn(dsn) as conn:
        return active_tenants(conn)
