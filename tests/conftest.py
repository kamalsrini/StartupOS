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

    base = dsn.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(base, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS startupos_test")
        c.execute("CREATE DATABASE startupos_test")
    from common.db import apply_schema

    apply_schema(dsn)
    return dsn


@pytest.fixture()
def conn(test_dsn):
    from common.db import ensure_tenant, get_conn

    with get_conn(test_dsn) as c:
        ensure_tenant(c, "unitone", "UnitOne", "https://unitone.ai")
        yield c
