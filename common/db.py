"""Postgres access. psycopg3, plain SQL, dict rows. One helper, no ORM."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from common.settings import settings

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "schema.sql"


@contextmanager
def get_conn(dsn: str | None = None) -> Iterator[psycopg.Connection]:
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
    with get_conn(dsn) as conn:
        conn.execute(SCHEMA_PATH.read_text())


def ensure_tenant(
    conn: psycopg.Connection, tenant_id: str, name: str | None = None, website: str | None = None
) -> None:
    conn.execute(
        """INSERT INTO tenants (id, name, website) VALUES (%s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET name = COALESCE(EXCLUDED.name, tenants.name),
                                          website = COALESCE(EXCLUDED.website, tenants.website)""",
        (tenant_id, name or tenant_id, website),
    )
