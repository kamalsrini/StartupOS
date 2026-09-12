"""Who is calling? Bearer token first, then the session cookie. The tenant is whatever the user row says."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import psycopg

from auth import sessions, tokens
from auth.config import app_dsn
from common.db import get_conn

Via = Literal["session", "token", "bootstrap"]


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    email: str
    name: str | None
    via: Via
    role: str = "owner"
    session_id: str | None = None
    token_id: str | None = None

    def as_user(self) -> dict[str, Any]:
        return {
            "id": self.user_id,
            "email": self.email,
            "name": self.name,
            "tenant_id": self.tenant_id,
            "role": self.role,
        }


def load_active_user(conn: psycopg.Connection, user_id: str) -> dict[str, Any] | None:
    """Tenant-bound read of the user row; None unless status == 'active'."""
    row = conn.execute(
        "SELECT id, tenant_id, email, name, role, status FROM users WHERE id = %s", (user_id,)
    ).fetchone()
    if not row or row["status"] != "active":
        return None
    return dict(row)


def bearer_from_header(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def resolve(authorization: str | None, session_cookie: str | None, dsn: str | None = None) -> Principal | None:
    """Order: Bearer token, then session cookie. Lookups run with NO tenant bound; the user row is read tenant-bound."""
    dsn = dsn or app_dsn()
    bearer = bearer_from_header(authorization)
    if bearer:
        with get_conn(dsn, tenant_id="") as anon:
            hit = tokens.lookup_token(anon, bearer)
        if hit:
            tenant_id, user_id, token_id = hit
            with get_conn(dsn, tenant_id=tenant_id) as conn:
                user = load_active_user(conn, user_id)
                if user:
                    tokens.touch_token(conn, token_id)
                    return Principal(
                        tenant_id, user_id, user["email"], user["name"], "token", user["role"], token_id=token_id
                    )
        return None  # a presented-but-invalid bearer never falls back to the cookie

    session_id = sessions.verify_cookie(session_cookie)
    if not session_id:
        return None
    with get_conn(dsn, tenant_id="") as anon:
        sess = sessions.load_session(anon, session_id)
    if not sess:
        return None
    with get_conn(dsn, tenant_id=sess["tenant_id"]) as conn:
        user = load_active_user(conn, sess["user_id"])
        if not user:
            return None
        sessions.touch_session(conn, session_id)
        return Principal(
            sess["tenant_id"], user["id"], user["email"], user["name"], "session", user["role"], session_id=session_id
        )


def resolve_principal(request: Any, dsn: str | None = None) -> Principal | None:
    """FastAPI/Starlette request → Principal | None."""
    return resolve(request.headers.get("authorization"), request.cookies.get(sessions.COOKIE_NAME), dsn)
