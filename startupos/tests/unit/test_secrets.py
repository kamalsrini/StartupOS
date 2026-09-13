"""common/secrets.py envelope primitives and settings.secret('kv:…') — pure, no Postgres."""

from __future__ import annotations

import base64
import os

import pytest

from common import secrets
from common.settings import settings


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def test_round_trip_and_blob_is_opaque():
    master = secrets.decode_master(_key())
    blob = secrets.encrypt(master, "acme", "linear_api_key", "lin_api_SECRET_123")
    assert isinstance(blob, bytes) and blob[:1] == secrets.VERSION
    assert b"lin_api" not in blob and b"SECRET" not in blob
    assert secrets.decrypt(master, "acme", "linear_api_key", blob) == "lin_api_SECRET_123"
    # random data key + nonces: the same value never encrypts to the same blob
    assert secrets.encrypt(master, "acme", "linear_api_key", "lin_api_SECRET_123") != blob
    # unicode survives
    b2 = secrets.encrypt(master, "acme", "n", "pässwörd ✓")
    assert secrets.decrypt(master, "acme", "n", b2) == "pässwörd ✓"


def test_wrong_master_key_fails_closed():
    good, bad = secrets.decode_master(_key()), secrets.decode_master(_key())
    blob = secrets.encrypt(good, "acme", "k", "v")
    with pytest.raises(secrets.SecretDecryptError):
        secrets.decrypt(bad, "acme", "k", blob)


def test_blob_is_bound_to_tenant_and_name():
    master = secrets.decode_master(_key())
    blob = secrets.encrypt(master, "acme", "linear_api_key", "v")
    with pytest.raises(secrets.SecretDecryptError):
        secrets.decrypt(master, "other-tenant", "linear_api_key", blob)
    with pytest.raises(secrets.SecretDecryptError):
        secrets.decrypt(master, "acme", "slack_api_key", blob)


def test_tampered_or_malformed_blob_fails_closed():
    master = secrets.decode_master(_key())
    blob = bytearray(secrets.encrypt(master, "t", "n", "value"))
    blob[-1] ^= 0x01  # flip a payload tag bit
    with pytest.raises(secrets.SecretDecryptError):
        secrets.decrypt(master, "t", "n", bytes(blob))
    with pytest.raises(secrets.SecretDecryptError):
        secrets.decrypt(master, "t", "n", b"\x00short")
    with pytest.raises(secrets.SecretDecryptError):
        secrets.decrypt(master, "t", "n", b"")


def test_rewrap_keeps_payload_and_switches_key():
    old, new = secrets.decode_master(_key()), secrets.decode_master(_key())
    blob = secrets.encrypt(old, "t", "n", "value")
    re = secrets.rewrap(old, new, "t", "n", blob)
    assert secrets.decrypt(new, "t", "n", re) == "value"
    assert re[-(len(blob) - secrets._HEADER_LEN) :] == blob[secrets._HEADER_LEN :]  # payload bytes untouched
    with pytest.raises(secrets.SecretDecryptError):
        secrets.decrypt(old, "t", "n", re)


@pytest.mark.parametrize("raw", [None, "", "not-base64!!", base64.urlsafe_b64encode(b"short").decode()])
def test_master_key_validation(raw):
    with pytest.raises(secrets.SecretsUnavailable):
        secrets.decode_master(raw)


def test_master_key_accepts_unpadded_and_padded():
    k = os.urandom(32)
    padded = base64.urlsafe_b64encode(k).decode()
    assert secrets.decode_master(padded) == k
    assert secrets.decode_master(padded.rstrip("=")) == k
    assert secrets.decode_master(k) == k


def test_no_master_key_means_unavailable(monkeypatch):
    monkeypatch.delenv("STARTUPOS_MASTER_KEY", raising=False)

    class Conn:  # must not be touched
        def execute(self, *a):
            raise AssertionError("table must not be read without a master key")

    with pytest.raises(secrets.SecretsUnavailable):
        secrets.put(Conn(), "t", "n", "v")
    with pytest.raises(secrets.SecretsUnavailable):
        secrets.get(Conn(), "t", "n")
    with pytest.raises(secrets.SecretsUnavailable):
        settings.secret("kv:n", conn=Conn(), tenant_id="t")


def test_settings_secret_env_unchanged(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "lin_x")
    assert settings.secret("env:LINEAR_API_KEY") == "lin_x"
    monkeypatch.delenv("LINEAR_API_KEY")
    assert settings.secret("env:LINEAR_API_KEY") is None
    with pytest.raises(NotImplementedError):
        settings.secret("vault:whatever")


def test_settings_secret_kv_requires_conn_and_tenant():
    with pytest.raises(ValueError):
        settings.secret("kv:linear_api_key")
    with pytest.raises(ValueError):
        settings.secret("kv:linear_api_key", conn=object())
    with pytest.raises(ValueError):
        settings.secret("kv:linear_api_key", tenant_id="t")


def test_settings_secret_kv_reads_tenant_row(monkeypatch):
    key = _key()
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", key)
    blob = secrets.encrypt(secrets.decode_master(key), "acme", "linear_api_key", "lin_live")
    seen: list[tuple] = []

    class Cur:
        def __init__(self, row):
            self.row = row

        def fetchone(self):
            return self.row

    class Conn:
        def execute(self, sql, params=()):
            seen.append(params)
            return Cur({"ciphertext": blob} if params == ("acme", "linear_api_key") else None)

    assert settings.secret("kv:linear_api_key", conn=Conn(), tenant_id="acme") == "lin_live"
    assert settings.secret("kv:linear_api_key", conn=Conn(), tenant_id="other") is None
    assert all(p[0] in ("acme", "other") for p in seen)  # always scoped by tenant_id


def test_operator_env_refs_match_the_ingest_modules():
    """common.secrets.OPERATOR_ENV_REFS is what the API validates against; ingest modules must agree."""
    from ingest import runner

    assert runner.SECRET_REFS == secrets.OPERATOR_ENV_REFS


def test_env_refs_resolve_only_for_the_install_tenant(monkeypatch):
    from common.settings import SecretRefForbidden

    monkeypatch.setenv("LINEAR_API_KEY", "lin_x")
    assert settings.secret("env:LINEAR_API_KEY") == "lin_x"
    assert settings.secret("env:LINEAR_API_KEY", tenant_id=settings.tenant_id) == "lin_x"
    with pytest.raises(SecretRefForbidden):
        settings.secret("env:LINEAR_API_KEY", tenant_id="someone-else")


def test_check_master_key_fails_loudly_on_a_malformed_key(monkeypatch):
    import logging

    log = logging.getLogger("test.secrets")
    monkeypatch.delenv("STARTUPOS_MASTER_KEY", raising=False)
    assert secrets.check_master_key(log) is False  # unset: warned, services still start (503 on credential writes)
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", "not-32-bytes")
    with pytest.raises(RuntimeError, match="STARTUPOS_MASTER_KEY"):
        secrets.check_master_key(log)
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    assert secrets.check_master_key(log) is True
