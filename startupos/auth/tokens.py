"""Personal API tokens: `sos_<12hex>_<32urlsafe>`. Only the sha256 hex is stored; the plaintext is shown once."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

import psycopg

TOKEN_RE = re.compile(r"^sos_([0-9a-f]{12})_([A-Za-z0-9_\-]{32,})$")
TOUCH_INTERVAL_MINUTES = 5


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def new_token() -> tuple[str, str]:
    """Return (plaintext, sha256hex). The middle segment doubles as the token id."""
    tid = secrets.token_hex(6)  # 12 hex chars
    secret = secrets.token_urlsafe(24)  # 32 urlsafe chars
    plaintext = f"sos_{tid}_{secret}"
    return plaintext, hash_token(plaintext)


def looks_like_token(value: str | None) -> bool:
    return bool(value and TOKEN_RE.match(value))


def create_token(conn: psycopg.Connection, tenant_id: str, user_id: str, name: str) -> tuple[str, str]:
    """Insert an api_tokens row (conn bound to `tenant_id`). Returns (token_id, plaintext) — plaintext is never stored."""
    plaintext, digest = new_token()
    token_id = "tok_" + plaintext.split("_")[1]
    conn.execute(
        "INSERT INTO api_tokens (id, tenant_id, user_id, name, token_hash) VALUES (%s, %s, %s, %s, %s)",
        (token_id, tenant_id, user_id, (name or "token")[:120], digest),
    )
    return token_id, plaintext


def lookup_token(conn_no_tenant: psycopg.Connection, plaintext: str) -> tuple[str, str, str] | None:
    """Pre-auth lookup by hash through auth_lookup_token → (tenant_id, user_id, token_id) or None."""
    if not looks_like_token(plaintext):
        return None
    row = conn_no_tenant.execute("SELECT * FROM auth_lookup_token(%s)", (hash_token(plaintext),)).fetchone()
    if not row:
        return None
    return row["tenant_id"], row["user_id"], row["token_id"]


def touch_token(conn: psycopg.Connection, token_id: str) -> bool:
    """Bump last_used_at at most once per TOUCH_INTERVAL_MINUTES."""
    cur = conn.execute(
        """UPDATE api_tokens SET last_used_at = now()
            WHERE id = %s AND (last_used_at IS NULL OR last_used_at < now() - (%s * interval '1 minute'))""",
        (token_id, TOUCH_INTERVAL_MINUTES),
    )
    return cur.rowcount > 0


def revoke_token(conn: psycopg.Connection, token_id: str, user_id: str | None = None) -> bool:
    sql = "UPDATE api_tokens SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL"
    params: list[Any] = [token_id]
    if user_id:
        sql += " AND user_id = %s"
        params.append(user_id)
    return conn.execute(sql, params).rowcount > 0


def list_tokens(conn: psycopg.Connection, user_id: str) -> list[dict[str, Any]]:
    """Never returns token_hash."""
    rows = conn.execute(
        """SELECT id, name, created_at, last_used_at, revoked_at FROM api_tokens
            WHERE user_id = %s ORDER BY created_at DESC""",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]
