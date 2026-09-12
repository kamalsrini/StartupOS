"""Browser sessions. The cookie carries only an HMAC-signed opaque id; state lives in `sessions` so it can be revoked."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from auth import config

COOKIE_NAME = "sos_session"
_SALT = "sos_session"
TOUCH_INTERVAL_MINUTES = 5


def ttl_seconds() -> int:
    return config.session_ttl_hours() * 3600


def _signer() -> TimestampSigner:
    return TimestampSigner(config.session_secret(), salt=_SALT)


def sign_session_id(session_id: str) -> str:
    return _signer().sign(session_id.encode()).decode()


def verify_cookie(value: str | None, max_age: int | None = None) -> str | None:
    """Signed cookie value → session id, or None when missing, tampered or older than the TTL."""
    if not value:
        return None
    try:
        raw = _signer().unsign(value.encode(), max_age=max_age or ttl_seconds())
    except (BadSignature, SignatureExpired, ValueError):
        return None
    sid = raw.decode(errors="ignore")
    return sid or None


def create_session(
    conn: psycopg.Connection, tenant_id: str, user_id: str, user_agent: str | None = None
) -> tuple[str, str]:
    """Insert a sessions row (conn must be bound to `tenant_id` or superuser). Returns (session_id, cookie_value)."""
    session_id = secrets.token_urlsafe(32)
    expires_at = datetime.now(tz=UTC) + timedelta(seconds=ttl_seconds())
    conn.execute(
        """INSERT INTO sessions (id, tenant_id, user_id, expires_at, user_agent)
           VALUES (%s, %s, %s, %s, %s)""",
        (session_id, tenant_id, user_id, expires_at, (user_agent or "")[:300] or None),
    )
    return session_id, sign_session_id(session_id)


def load_session(conn_no_tenant: psycopg.Connection, session_id: str) -> dict[str, Any] | None:
    """Pre-auth lookup through the SECURITY DEFINER function. Returns {tenant_id, user_id} for a live session."""
    row = conn_no_tenant.execute("SELECT * FROM auth_lookup_session(%s)", (session_id,)).fetchone()
    if not row:
        return None
    if row["revoked_at"] is not None:
        return None
    if row["expires_at"] is None or row["expires_at"] <= datetime.now(tz=UTC):
        return None
    return {"tenant_id": row["tenant_id"], "user_id": row["user_id"]}


def touch_session(conn: psycopg.Connection, session_id: str) -> bool:
    """Bump last_seen_at at most once per TOUCH_INTERVAL_MINUTES (the WHERE clause is the rate limit)."""
    cur = conn.execute(
        "UPDATE sessions SET last_seen_at = now() WHERE id = %s AND last_seen_at < now() - (%s * interval '1 minute')",
        (session_id, TOUCH_INTERVAL_MINUTES),
    )
    return cur.rowcount > 0


def revoke_session(conn: psycopg.Connection, session_id: str) -> bool:
    cur = conn.execute("UPDATE sessions SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (session_id,))
    return cur.rowcount > 0
