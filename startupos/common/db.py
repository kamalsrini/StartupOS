"""Postgres access. psycopg3, plain SQL, dict rows. One helper, no ORM.

Tenant isolation: every connection sets `app.tenant_id`; Row Level Security (db/rls.sql) filters every
tenant table by it. Services connect as `startupos_app` (NOBYPASSRLS) via STARTUPOS_APP_DSN when set;
the superuser DSN is for migrations and operators.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from common.settings import settings

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "schema.sql"
RLS_PATH = Path(__file__).resolve().parents[1] / "db" / "rls.sql"


def set_tenant(conn: psycopg.Connection, tenant_id: str | None) -> None:
    """Bind this connection to one tenant for RLS. Session-level so it survives commits."""
    conn.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant_id or "",))


@contextmanager
def get_conn(dsn: str | None = None, tenant_id: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a connection bound to `tenant_id` (default: settings.tenant_id). Commits on clean exit."""
    conn = psycopg.connect(dsn or settings.app_dsn or settings.database_url, row_factory=dict_row)
    try:
        set_tenant(conn, tenant_id if tenant_id is not None else settings.tenant_id)
        # Commit the binding on its own: psycopg opens a transaction on the first statement, and a later
        # rollback() (ingest records a failure that way) would otherwise undo set_config and leave the
        # connection unbound — every following query would then be refused by RLS.
        conn.commit()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def admin_conn(dsn: str | None = None) -> Iterator[psycopg.Connection]:
    """Superuser/operator connection (migrations, tenant creation). Never used by request handlers."""
    conn = psycopg.connect(dsn or settings.database_url, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_schema(dsn: str | None = None) -> None:
    with admin_conn(dsn) as conn:
        conn.execute(SCHEMA_PATH.read_text())
        conn.execute(RLS_PATH.read_text())


def ensure_tenant(
    conn: psycopg.Connection, tenant_id: str, name: str | None = None, website: str | None = None
) -> None:
    """Create/refresh a tenant row. Requires the connection to be bound to `tenant_id` (RLS WITH CHECK) or superuser."""
    conn.execute(
        """INSERT INTO tenants (id, name, website) VALUES (%s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET name = COALESCE(EXCLUDED.name, tenants.name),
                                          website = COALESCE(EXCLUDED.website, tenants.website)""",
        (tenant_id, name or tenant_id, website),
    )
