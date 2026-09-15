"""Shared fixtures. Functional tests get a fresh schema on STARTUPOS_TEST_DSN per session."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(scope="session")
def fixtures():
    return load_fixture


@pytest.fixture(scope="session")
def test_dsn():
    dsn = os.environ.get("STARTUPOS_TEST_DSN", "postgresql://postgres@localhost:5432/startupos_test")
    import psycopg

    base, dbname = dsn.rsplit("/", 1)
    base += "/postgres"
    with psycopg.connect(base, autocommit=True) as c:
        # dbname comes from STARTUPOS_TEST_DSN so parallel checkouts can each own a database
        c.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
        c.execute(f'CREATE DATABASE "{dbname}"')
    from common.db import apply_schema

    apply_schema(dsn)
    return dsn


_TEST_DB = os.environ.get("STARTUPOS_TEST_DSN", "postgresql://postgres@localhost:5432/startupos_test").rsplit("/", 1)[1]
APP_TEST_DSN = f"postgresql://startupos_app:startupos_app@localhost:5432/{_TEST_DB}"


@pytest.fixture()
def conn(test_dsn):
    """Superuser connection bound to tenant 'unitone' (bypasses RLS — for seeding and legacy tests)."""
    from common.db import ensure_tenant, get_conn

    with get_conn(test_dsn, tenant_id="unitone") as c:
        ensure_tenant(c, "unitone", "UnitOne", "https://unitone.ai")
        yield c


@pytest.fixture()
def app_conn(test_dsn):
    """RLS-bound connection as the startupos_app role, tenant 'unitone'. Use to prove isolation."""
    from common.db import get_conn

    dsn = os.environ.get("STARTUPOS_APP_TEST_DSN", APP_TEST_DSN)
    with get_conn(dsn, tenant_id="unitone") as c:
        yield c


def app_conn_for(tenant_id: str):
    from common.db import get_conn

    return get_conn(os.environ.get("STARTUPOS_APP_TEST_DSN", APP_TEST_DSN), tenant_id=tenant_id)


TEST_SESSION_SECRET = "test-session-secret-not-for-production"


@pytest.fixture()
def auth_env(monkeypatch, test_dsn):
    """Point the API at the scratch DB and give it a session secret + bootstrap token."""
    monkeypatch.setenv("STARTUPOS_API_DSN", os.environ.get("STARTUPOS_API_DSN_OVERRIDE", test_dsn))
    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("STARTUPOS_BOOTSTRAP_TOKEN", "bootstrap-test-token")
    monkeypatch.delenv("STARTUPOS_DEV", raising=False)
    return test_dsn


def login_as(conn, tenant_id: str, email: str, name: str | None = None) -> tuple[str, str]:
    """Seed an active owner in `tenant_id` (superuser conn) and mint a session. Returns (user_id, cookie_value)."""
    from auth import bootstrap, sessions

    user = bootstrap.upsert_owner(conn, tenant_id, email, name)
    _, cookie = sessions.create_session(conn, tenant_id, user["id"], "pytest")
    conn.commit()
    return user["id"], cookie


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Rate limiting is a property of the deployment, not of the case under test.

    `POST /auth/bootstrap` and `POST /onboarding/tenant` are throttled per process (api/ratelimit.py), and a
    suite creates far more companies in a minute than any founder does. Clear the buckets around every test so
    the limiters are exercised only by the tests that mean to, and never leak into the ones that do not.
    """
    try:
        from api import ratelimit
    except Exception:  # the API package is not importable in every environment; nothing to reset
        yield
        return
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()
