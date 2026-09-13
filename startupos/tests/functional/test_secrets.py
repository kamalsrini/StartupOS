"""Track S — per-tenant secrets against Postgres: put/get, RLS isolation via startupos_app, master rotation,
and the connections API storing a credential without ever echoing it."""

from __future__ import annotations

import base64
import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from common import secrets
from common.settings import SecretRefForbidden, settings
from tests.conftest import app_conn_for, login_as

pytestmark = pytest.mark.functional

TA, TB = "sec-tenant-a", "sec-tenant-b"


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


@pytest.fixture()
def master(monkeypatch):
    key = _key()
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", key)
    return key


@pytest.fixture()
def two_tenants(conn):
    for t in (TA, TB):
        conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (t, t))
    conn.execute("DELETE FROM tenant_secrets WHERE tenant_id IN (%s, %s)", (TA, TB))
    conn.commit()
    yield
    conn.execute("DELETE FROM tenant_secrets WHERE tenant_id IN (%s, %s)", (TA, TB))
    conn.commit()


def test_put_get_delete_round_trip(conn, master, two_tenants):
    secrets.put(conn, TA, "linear_api_key", "lin_api_AAA")
    assert secrets.get(conn, TA, "linear_api_key") == "lin_api_AAA"
    assert secrets.exists(conn, TA, "linear_api_key")
    # replace in place
    secrets.put(conn, TA, "linear_api_key", "lin_api_AAA2")
    assert secrets.get(conn, TA, "linear_api_key") == "lin_api_AAA2"
    assert conn.execute("SELECT count(*) AS n FROM tenant_secrets WHERE tenant_id=%s", (TA,)).fetchone()["n"] == 1
    # nothing on disk resembles the plaintext
    blob = conn.execute("SELECT ciphertext FROM tenant_secrets WHERE tenant_id=%s", (TA,)).fetchone()["ciphertext"]
    assert b"lin_api" not in bytes(blob)
    assert secrets.get(conn, TA, "missing") is None
    assert secrets.delete(conn, TA, "linear_api_key") is True
    assert secrets.delete(conn, TA, "linear_api_key") is False
    assert secrets.get(conn, TA, "linear_api_key") is None


def test_rls_tenant_b_cannot_read_tenant_a(conn, master, two_tenants):
    with app_conn_for(TA) as a:
        secrets.put(a, TA, "linear_api_key", "lin_api_A_ONLY")
        assert secrets.get(a, TA, "linear_api_key") == "lin_api_A_ONLY"
        assert settings.secret("kv:linear_api_key", conn=a, tenant_id=TA) == "lin_api_A_ONLY"
    with app_conn_for(TB) as b:
        assert secrets.get(b, TB, "linear_api_key") is None
        assert settings.secret("kv:linear_api_key", conn=b, tenant_id=TB) is None
        # even asking for A's row by id from B's connection yields nothing
        assert b.execute("SELECT count(*) AS n FROM tenant_secrets").fetchone()["n"] == 0
        assert secrets.get(b, TA, "linear_api_key") is None
        # and B cannot smuggle a row into A
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            secrets.put(b, TA, "linear_api_key", "overwritten")
        b.rollback()
    with app_conn_for("") as none:
        assert none.execute("SELECT count(*) AS n FROM tenant_secrets").fetchone()["n"] == 0
    # A's value is intact after B's attempts
    with app_conn_for(TA) as a:
        assert secrets.get(a, TA, "linear_api_key") == "lin_api_A_ONLY"


def test_wrong_master_key_fails_closed_on_read(conn, monkeypatch, two_tenants):
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", _key())
    secrets.put(conn, TA, "k", "v")
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", _key())
    with pytest.raises(secrets.SecretDecryptError):
        secrets.get(conn, TA, "k")
    monkeypatch.delenv("STARTUPOS_MASTER_KEY")
    with pytest.raises(secrets.SecretsUnavailable):
        secrets.get(conn, TA, "k")


def test_rotate_master_rewraps_every_tenant(conn, monkeypatch, two_tenants):
    old, new = _key(), _key()
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", old)
    secrets.put(conn, TA, "linear_api_key", "a-linear")
    secrets.put(conn, TA, "slack_api_key", "a-slack")
    secrets.put(conn, TB, "linear_api_key", "b-linear")
    before = {
        (r["tenant_id"], r["name"]): bytes(r["ciphertext"])
        for r in conn.execute("SELECT * FROM tenant_secrets WHERE tenant_id IN (%s,%s)", (TA, TB))
    }
    assert secrets.rotate_master(conn, old, new) >= 3  # superuser conn sees every tenant
    conn.commit()
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", new)
    assert secrets.get(conn, TA, "linear_api_key") == "a-linear"
    assert secrets.get(conn, TA, "slack_api_key") == "a-slack"
    with app_conn_for(TB) as b:
        assert secrets.get(b, TB, "linear_api_key") == "b-linear"
    after = {
        (r["tenant_id"], r["name"]): bytes(r["ciphertext"])
        for r in conn.execute("SELECT * FROM tenant_secrets WHERE tenant_id IN (%s,%s)", (TA, TB))
    }
    assert set(after) == set(before) and all(after[k] != before[k] for k in before)
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", old)
    with pytest.raises(secrets.SecretDecryptError):
        secrets.get(conn, TA, "linear_api_key")


# --- API ---------------------------------------------------------------------------

T_API = "sec-api-tenant"
PLAINTEXT = "lin_api_SUPERSECRET_xyz789"


@pytest.fixture()
def client(conn, auth_env):
    from common.db import ensure_tenant

    ensure_tenant(conn, T_API, "Secrets API Co", "https://secrets.test")
    for t in ("sessions", "api_tokens", "connections", "tenant_secrets"):
        conn.execute(f"DELETE FROM {t} WHERE tenant_id = %s", (T_API,))
    conn.commit()
    _, cookie = login_as(conn, T_API, "owner@secrets.test")
    from api.main import app

    with TestClient(app) as c:
        c.cookies.set("sos_session", cookie)
        yield c
    for t in ("connections", "tenant_secrets"):
        conn.execute(f"DELETE FROM {t} WHERE tenant_id = %s", (T_API,))
    conn.commit()


def test_api_stores_credential_and_never_echoes_it(client, conn, master):
    r = client.post("/onboarding/connections", json={"source": "linear", "credential": PLAINTEXT})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["secret_ref"] == "kv:linear_api_key" and body["has_credential"] is True
    assert "credential" not in body and PLAINTEXT not in r.text and "SUPERSECRET" not in r.text

    r = client.get("/onboarding/connections")
    assert r.status_code == 200
    assert PLAINTEXT not in r.text and "SUPERSECRET" not in r.text
    (row,) = [c for c in r.json() if c["source"] == "linear"]
    assert row["secret_ref"] == "kv:linear_api_key" and row["has_credential"] is True
    assert "credential" not in row and "ciphertext" not in row

    # stored encrypted for this tenant, readable only through secrets.get under the master key
    stored = conn.execute("SELECT * FROM tenant_secrets WHERE tenant_id=%s", (T_API,)).fetchall()
    assert [s["name"] for s in stored] == ["linear_api_key"]
    assert PLAINTEXT.encode() not in bytes(stored[0]["ciphertext"])
    assert secrets.get(conn, T_API, "linear_api_key") == PLAINTEXT
    with app_conn_for(T_API) as a:
        assert settings.secret("kv:linear_api_key", conn=a, tenant_id=T_API) == PLAINTEXT
    with app_conn_for("unitone") as other:
        assert settings.secret("kv:linear_api_key", conn=other, tenant_id="unitone") is None

    # credential wins over a secret_ref given alongside; re-posting rotates the stored value in place
    r = client.post(
        "/onboarding/connections", json={"source": "linear", "credential": PLAINTEXT + "2", "secret_ref": "env:X"}
    )
    assert r.status_code == 200 and r.json()["secret_ref"] == "kv:linear_api_key"
    assert secrets.get(conn, T_API, "linear_api_key") == PLAINTEXT + "2"
    assert conn.execute("SELECT count(*) AS n FROM tenant_secrets WHERE tenant_id=%s", (T_API,)).fetchone()["n"] == 1

    # secret_ref-only connections still work and report has_credential honestly
    r = client.post("/onboarding/connections", json={"source": "slack", "secret_ref": "kv:slack_api_key"})
    assert r.status_code == 200 and r.json()["has_credential"] is False
    assert client.post("/onboarding/connections", json={"source": "brex", "credential": "   "}).status_code == 422
    assert client.post("/onboarding/connections", json={"source": "brex"}).status_code == 422
    assert client.post("/onboarding/connections", json={"source": "brex", "secret_ref": PLAINTEXT}).status_code == 422


def test_api_returns_503_without_master_key(client, conn, monkeypatch):
    monkeypatch.delenv("STARTUPOS_MASTER_KEY", raising=False)
    r = client.post("/onboarding/connections", json={"source": "linear", "credential": PLAINTEXT})
    assert r.status_code == 503, r.text
    assert PLAINTEXT not in r.text
    # nothing half-written
    assert conn.execute("SELECT count(*) AS n FROM connections WHERE tenant_id=%s", (T_API,)).fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM tenant_secrets WHERE tenant_id=%s", (T_API,)).fetchone()["n"] == 0
    # secret_ref-only writes do not need the master key
    r = client.post("/onboarding/connections", json={"source": "slack", "secret_ref": "kv:slack_api_key"})
    assert r.status_code == 200
    # and listing never needs to decrypt
    assert client.get("/onboarding/connections").status_code == 200


def test_env_refs_are_the_install_tenants_only(client, conn, monkeypatch):
    """PE review (Sprint 3a): a self-serve tenant pointing a connection at `env:LINEAR_API_KEY` would ingest and act
    with the operator's Linear. env: refs are accepted only for the install tenant (TENANT_ID) and only at the
    source's canonical variable; `has_credential` never reports the environment to another tenant."""
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_OPERATOR")
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", _key())
    assert settings.tenant_id != T_API
    for ref in ("env:LINEAR_API_KEY", "env:STARTUPOS_MASTER_KEY", "env:PATH"):
        r = client.post("/onboarding/connections", json={"source": "linear", "secret_ref": ref})
        assert r.status_code == 403, (ref, r.text)
        assert "lin_api" not in r.text
    assert conn.execute("SELECT count(*) AS n FROM connections WHERE tenant_id=%s", (T_API,)).fetchone()["n"] == 0
    # a row that somehow carries an env ref (pre-3a data) neither resolves nor reports the operator's key
    conn.execute(
        "INSERT INTO connections (id, tenant_id, source, secret_ref, status) VALUES (%s, %s, 'linear', 'env:LINEAR_API_KEY', 'connected')",
        (f"{T_API}:linear", T_API),
    )
    conn.commit()
    (row,) = [c for c in client.get("/onboarding/connections").json() if c["source"] == "linear"]
    assert row["has_credential"] is False
    with app_conn_for(T_API) as a:
        with pytest.raises(SecretRefForbidden):
            settings.secret("env:LINEAR_API_KEY", conn=a, tenant_id=T_API)
        with pytest.raises(SecretRefForbidden):
            secrets.credential_for_source(a, T_API, "linear")
    # the install tenant itself: canonical ref accepted, arbitrary env names refused, executors resolve the key
    with app_conn_for(settings.tenant_id) as op:
        op.execute("DELETE FROM connections WHERE tenant_id = %s AND source = 'linear'", (settings.tenant_id,))
        assert secrets.credential_for_source(op, settings.tenant_id, "linear") == "lin_api_OPERATOR"  # env fallback
    assert secrets.env_ref_allowed(settings.tenant_id, "linear", "env:LINEAR_API_KEY")
    assert not secrets.env_ref_allowed(settings.tenant_id, "linear", "env:STARTUPOS_MASTER_KEY")
    assert not secrets.env_ref_allowed(settings.tenant_id, "slack", "env:LINEAR_API_KEY")
