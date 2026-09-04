"""First-owner bootstrap for the single-operator install (before Google is configured).

Guarded by STARTUPOS_BOOTSTRAP_TOKEN (constant-time compare). Disabled when the env var is empty.
"""

from __future__ import annotations

import hmac
from typing import Any

import psycopg

from auth import config
from common.db import ensure_tenant, set_tenant


def enabled() -> bool:
    return bool(config.bootstrap_token())


def check_token(presented: str | None) -> bool:
    expected = config.bootstrap_token()
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def user_id_for(tenant_id: str, email: str) -> str:
    return f"{tenant_id}:{email.strip().lower()}"


def upsert_owner(conn: psycopg.Connection, tenant_id: str, email: str, name: str | None = None) -> dict[str, Any]:
    """Create or (re)activate the owner user for `tenant_id`. `conn` must be bound to the tenant or superuser."""
    email = email.strip().lower()
    row = conn.execute(
        """INSERT INTO users (id, tenant_id, email, name, role, status)
           VALUES (%s, %s, %s, %s, 'owner', 'active')
           ON CONFLICT (tenant_id, email) DO UPDATE
             SET role = 'owner', status = 'active', name = COALESCE(EXCLUDED.name, users.name)
           RETURNING id, tenant_id, email, name, role, status""",
        (user_id_for(tenant_id, email), tenant_id, email, (name or "").strip() or None),
    ).fetchone()
    return dict(row)


def bootstrap(
    conn_admin: psycopg.Connection,
    tenant_id: str,
    email: str,
    name: str | None = None,
    *,
    tenant_name: str | None = None,
    website: str | None = None,
) -> dict[str, Any]:
    """Create the tenant if missing and its active owner. Binds the connection to `tenant_id` (RLS WITH CHECK)."""
    set_tenant(conn_admin, tenant_id)
    ensure_tenant(conn_admin, tenant_id, tenant_name, website)
    return upsert_owner(conn_admin, tenant_id, email, name)
