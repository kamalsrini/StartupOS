"""API token format, hashing, and revoke (DB-backed parts use a fake connection)."""

from __future__ import annotations

import hashlib

from auth import tokens


def test_new_token_format_and_hash():
    plaintext, digest = tokens.new_token()
    assert tokens.TOKEN_RE.match(plaintext), plaintext
    head, tid, secret = plaintext.split("_", 2)
    assert head == "sos" and len(tid) == 12 and len(secret) == 32
    assert digest == hashlib.sha256(plaintext.encode()).hexdigest()
    assert len({tokens.new_token()[0] for _ in range(50)}) == 50


def test_looks_like_token():
    assert tokens.looks_like_token("sos_0123456789ab_" + "a" * 32)
    assert not tokens.looks_like_token("sos_short_x")
    assert not tokens.looks_like_token("Bearer sos_0123456789ab_" + "a" * 32)
    assert not tokens.looks_like_token(None)


class _Cur:
    def __init__(self, rowcount=0, row=None):
        self.rowcount = rowcount
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row] if self._row else []


class _Conn:
    def __init__(self, row=None, rowcount=1):
        self.calls: list[tuple[str, tuple]] = []
        self.row = row
        self.rowcount = rowcount

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        return _Cur(self.rowcount, self.row)


def test_create_token_stores_hash_not_plaintext():
    conn = _Conn()
    token_id, plaintext = tokens.create_token(conn, "unitone", "u1", "cli")
    sql, params = conn.calls[0]
    assert "INSERT INTO api_tokens" in sql
    assert plaintext not in params and tokens.hash_token(plaintext) in params
    assert token_id == "tok_" + plaintext.split("_")[1]


def test_lookup_uses_security_definer_function_with_hash():
    plaintext, digest = tokens.new_token()
    conn = _Conn(row={"tenant_id": "unitone", "user_id": "u1", "token_id": "tok_x"})
    assert tokens.lookup_token(conn, plaintext) == ("unitone", "u1", "tok_x")
    sql, params = conn.calls[0]
    assert "auth_lookup_token" in sql and params == (digest,)
    assert tokens.lookup_token(conn, "not-a-token") is None
    assert tokens.lookup_token(_Conn(row=None), plaintext) is None


def test_revoke_scoped_to_user():
    conn = _Conn(rowcount=1)
    assert tokens.revoke_token(conn, "tok_x", "u1") is True
    sql, params = conn.calls[0]
    assert "revoked_at = now()" in sql and "user_id" in sql and params == ("tok_x", "u1")
    assert tokens.revoke_token(_Conn(rowcount=0), "tok_x", "u1") is False


def test_list_tokens_never_returns_hash():
    conn = _Conn(row={"id": "tok_x", "name": "cli", "created_at": None, "last_used_at": None, "revoked_at": None})
    rows = tokens.list_tokens(conn, "u1")
    assert rows and "token_hash" not in rows[0]
    assert "token_hash" not in conn.calls[0][0].split("FROM")[0]
