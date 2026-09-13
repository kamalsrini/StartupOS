"""Per-tenant secrets: envelope encryption over the `tenant_secrets` table (Sprint 3a, Track S).

Layout of one row's `ciphertext` (all lengths fixed, so the blob is self-describing):

    version(1) | wrap_nonce(12) | wrapped_data_key(32 + 16 tag) | payload_nonce(12) | AES-GCM(data_key, value) + tag

- The master key (`STARTUPOS_MASTER_KEY`, 32 bytes urlsafe-base64) only ever wraps per-row random data keys.
- Both AES-GCM layers use `"<tenant_id>:<name>"` as associated data, so a blob copied onto another tenant's or
  another name's row fails to decrypt instead of quietly leaking a credential across rows.
- Rotation re-wraps only the data keys; payloads are untouched.
- Fail closed: no/malformed master key → `SecretsUnavailable`; a wrong master key or tampered blob →
  `SecretDecryptError`. Neither ever returns a plaintext or a partial one.

Plaintext values are never logged and never leave this module except through `get()`.
"""

from __future__ import annotations

import base64
import binascii
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from common.settings import settings

VERSION = b"\x01"
KEY_LEN = 32
# The operator-managed env refs, one per source (ingest.<source>.KEY_REF must agree — unit-tested). They are the
# install tenant's (TENANT_ID) own keys: the only `env:` refs the API accepts on a connection, and only for that tenant.
OPERATOR_ENV_REFS: dict[str, str] = {
    "linear": "env:LINEAR_API_KEY",
    "slack": "env:SLACK_BOT_TOKEN",
    "brex": "env:BREX_API_TOKEN",
    "vercel": "env:VERCEL_TOKEN",
}
NONCE_LEN = 12
TAG_LEN = 16
_WRAPPED_LEN = KEY_LEN + TAG_LEN
_HEADER_LEN = len(VERSION) + NONCE_LEN + _WRAPPED_LEN + NONCE_LEN


class SecretsError(Exception):
    """Base class; nothing in here ever carries a plaintext in its message."""


class SecretsUnavailable(SecretsError):
    """No usable STARTUPOS_MASTER_KEY: secrets cannot be read or written (API answers 503)."""


class SecretDecryptError(SecretsError):
    """The blob does not decrypt under this master key (wrong key, tampering, or a row moved between tenants)."""


# --- key handling ------------------------------------------------------------


def decode_master(raw: str | bytes | None) -> bytes:
    """Turn the env value (urlsafe base64 of 32 bytes) into key bytes. Raises SecretsUnavailable otherwise."""
    if raw is None or raw == "" or raw == b"":
        raise SecretsUnavailable("STARTUPOS_MASTER_KEY is not set")
    if isinstance(raw, bytes) and len(raw) == KEY_LEN:
        return raw
    text = raw.decode() if isinstance(raw, bytes) else raw
    text = text.strip()
    try:
        key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as e:
        raise SecretsUnavailable("STARTUPOS_MASTER_KEY is not valid base64") from e
    if len(key) != KEY_LEN:
        raise SecretsUnavailable(f"STARTUPOS_MASTER_KEY must decode to {KEY_LEN} bytes, got {len(key)}")
    return key


def master_key() -> bytes:
    return decode_master(settings.master_key())


def check_master_key(log: Any) -> bool:
    """Startup check for every service: a malformed STARTUPOS_MASTER_KEY is a hard error (every credential write
    and every per-tenant ingest would otherwise fail one by one); an unset one is logged once with the consequence.
    Returns whether per-tenant secrets are usable."""
    raw = settings.master_key()
    if not raw:
        log.warning(
            "STARTUPOS_MASTER_KEY is not set: per-tenant credentials cannot be stored or read "
            "(POST /onboarding/connections {credential} answers 503; kv: connections fail their ingest). "
            'Generate one: python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
        )
        return False
    try:
        decode_master(raw)
    except SecretsUnavailable as e:
        raise RuntimeError(f"STARTUPOS_MASTER_KEY is set but unusable: {e}") from None
    return True


def _aad(tenant_id: str, name: str) -> bytes:
    return f"{tenant_id}:{name}".encode()


# --- pure envelope primitives (no DB) ---------------------------------------------


def encrypt(master: bytes, tenant_id: str, name: str, value: str) -> bytes:
    """value → self-describing blob under `master` (see module docstring)."""
    if not isinstance(value, str):
        raise TypeError("secret values are str")
    aad = _aad(tenant_id, name)
    data_key = os.urandom(KEY_LEN)
    wrap_nonce = os.urandom(NONCE_LEN)
    wrapped = AESGCM(master).encrypt(wrap_nonce, data_key, aad)
    payload_nonce = os.urandom(NONCE_LEN)
    payload = AESGCM(data_key).encrypt(payload_nonce, value.encode(), aad)
    return VERSION + wrap_nonce + wrapped + payload_nonce + payload


def _split(blob: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    if not isinstance(blob, bytes | bytearray | memoryview):
        raise SecretDecryptError("ciphertext is not bytes")
    blob = bytes(blob)
    if len(blob) < _HEADER_LEN + TAG_LEN or blob[:1] != VERSION:
        raise SecretDecryptError("ciphertext is malformed or of an unknown version")
    i = len(VERSION)
    wrap_nonce = blob[i : i + NONCE_LEN]
    i += NONCE_LEN
    wrapped = blob[i : i + _WRAPPED_LEN]
    i += _WRAPPED_LEN
    payload_nonce = blob[i : i + NONCE_LEN]
    i += NONCE_LEN
    return wrap_nonce, wrapped, payload_nonce, blob[i:]


def _unwrap(master: bytes, aad: bytes, wrap_nonce: bytes, wrapped: bytes) -> bytes:
    try:
        return AESGCM(master).decrypt(wrap_nonce, wrapped, aad)
    except InvalidTag as e:
        raise SecretDecryptError("data key does not unwrap under this master key") from e


def decrypt(master: bytes, tenant_id: str, name: str, blob: bytes) -> str:
    """blob → value. Fails closed with SecretDecryptError on a wrong key, tampering, or a moved row."""
    aad = _aad(tenant_id, name)
    wrap_nonce, wrapped, payload_nonce, payload = _split(blob)
    data_key = _unwrap(master, aad, wrap_nonce, wrapped)
    try:
        return AESGCM(data_key).decrypt(payload_nonce, payload, aad).decode()
    except InvalidTag as e:
        raise SecretDecryptError("payload does not authenticate") from e


def rewrap(old: bytes, new: bytes, tenant_id: str, name: str, blob: bytes) -> bytes:
    """Re-wrap the data key under `new` without touching the payload."""
    aad = _aad(tenant_id, name)
    wrap_nonce, wrapped, payload_nonce, payload = _split(blob)
    data_key = _unwrap(old, aad, wrap_nonce, wrapped)
    new_nonce = os.urandom(NONCE_LEN)
    return VERSION + new_nonce + AESGCM(new).encrypt(new_nonce, data_key, aad) + payload_nonce + payload


# --- table access (tenant-bound connection; RLS does the isolation) ----------------


def put(conn: Any, tenant_id: str, name: str, value: str) -> None:
    """Store/replace `name` for `tenant_id`. Raises SecretsUnavailable without a master key."""
    if not name:
        raise ValueError("secret name is required")
    blob = encrypt(master_key(), tenant_id, name, value)
    conn.execute(
        """INSERT INTO tenant_secrets (tenant_id, name, ciphertext) VALUES (%s, %s, %s)
           ON CONFLICT (tenant_id, name) DO UPDATE SET ciphertext = EXCLUDED.ciphertext, updated_at = now()""",
        (tenant_id, name, blob),
    )


def get(conn: Any, tenant_id: str, name: str) -> str | None:
    """Decrypt `name` for `tenant_id`; None when no row (RLS makes another tenant's row look absent)."""
    master = master_key()  # fail closed before touching the table
    row = conn.execute(
        "SELECT ciphertext FROM tenant_secrets WHERE tenant_id = %s AND name = %s", (tenant_id, name)
    ).fetchone()
    if not row:
        return None
    return decrypt(master, tenant_id, name, row["ciphertext"])


def exists(conn: Any, tenant_id: str, name: str) -> bool:
    """Is a value stored (without decrypting it — no master key needed)."""
    return (
        conn.execute("SELECT 1 FROM tenant_secrets WHERE tenant_id = %s AND name = %s", (tenant_id, name)).fetchone()
        is not None
    )


def delete(conn: Any, tenant_id: str, name: str) -> bool:
    cur = conn.execute("DELETE FROM tenant_secrets WHERE tenant_id = %s AND name = %s", (tenant_id, name))
    return cur.rowcount > 0


def env_ref_allowed(tenant_id: str, source: str, ref: str) -> bool:
    """May `tenant_id` point its `source` connection at `ref` when it is an `env:` ref? Only the install tenant, and
    only at that source's canonical operator variable — never another tenant, never an arbitrary env name (which
    would let a tenant probe the environment or hand STARTUPOS_MASTER_KEY/ANTHROPIC_API_KEY to a third-party API)."""
    return tenant_id == settings.tenant_id and ref == OPERATOR_ENV_REFS.get(source)


def credential_for_source(conn: Any, tenant_id: str, source: str) -> str | None:
    """The credential behind `tenant_id`'s `connections` row for `source` — what an executor acts with.

    Resolved through `settings.secret(ref, conn=, tenant_id=)` on the tenant-bound connection, so a `kv:` ref is
    this tenant's row only and an `env:` ref resolves only for the install tenant (SecretRefForbidden otherwise).
    The install tenant with no connection row falls back to its operator env ref (single-operator installs never
    ran onboarding). None = nothing to act with; callers must fail the action, never fall back to the environment.
    """
    row = conn.execute(
        "SELECT secret_ref FROM connections WHERE tenant_id = %s AND source = %s", (tenant_id, source)
    ).fetchone()
    ref = (row or {}).get("secret_ref") or ""
    if not ref and tenant_id == settings.tenant_id:
        ref = OPERATOR_ENV_REFS.get(source, "")
    if not ref:
        return None
    return settings.secret(ref, conn=conn, tenant_id=tenant_id)


def rotate_master(conn: Any, old: str | bytes, new: str | bytes) -> int:
    """Re-wrap every row's data key from `old` to `new` in one transaction. Needs a superuser (RLS-bypassing)
    connection so every tenant's rows are visible. Returns the number of rows rewrapped. A single row that does not
    unwrap under `old` aborts the rotation (nothing is committed) rather than leaving a mixed-key table."""
    old_key, new_key = decode_master(old), decode_master(new)
    rows = conn.execute("SELECT tenant_id, name, ciphertext FROM tenant_secrets ORDER BY tenant_id, name").fetchall()
    n = 0
    for r in rows:
        blob = rewrap(old_key, new_key, r["tenant_id"], r["name"], r["ciphertext"])
        conn.execute(
            "UPDATE tenant_secrets SET ciphertext = %s, updated_at = now() WHERE tenant_id = %s AND name = %s",
            (blob, r["tenant_id"], r["name"]),
        )
        n += 1
    return n
