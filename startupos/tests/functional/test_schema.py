import pytest

pytestmark = pytest.mark.functional


def test_schema_applies_and_tenant_exists(conn):
    row = conn.execute("select id from tenants where id='unitone'").fetchone()
    assert row["id"] == "unitone"
    tables = {r["tablename"] for r in conn.execute("select tablename from pg_tables where schemaname='public'")}
    assert {
        "brain_docs",
        "issues",
        "bills",
        "signals",
        "approvals",
        "runs",
        "budgets",
        "context_packs",
        "tenant_secrets",
    } <= tables
