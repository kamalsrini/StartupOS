"""Tenant isolation proof: the app role sees only its tenant; superuser seeding is invisible across tenants."""

import pytest

from tests.conftest import app_conn_for

pytestmark = pytest.mark.functional


def test_app_role_is_isolated_per_tenant(conn):
    conn.execute("INSERT INTO tenants (id, name) VALUES ('other', 'Other Co') ON CONFLICT (id) DO NOTHING")
    conn.execute("DELETE FROM issues WHERE id IN ('RLS-1','RLS-2')")
    conn.execute(
        "INSERT INTO issues (tenant_id, id, title) VALUES ('unitone','RLS-1','mine'), ('other','RLS-2','theirs')"
    )
    conn.commit()
    with app_conn_for("unitone") as a:
        ids = {r["id"] for r in a.execute("SELECT id FROM issues WHERE id LIKE 'RLS-%'")}
        assert ids == {"RLS-1"}
        assert a.execute("SELECT count(*) AS n FROM tenants").fetchone()["n"] == 1
    with app_conn_for("other") as b:
        assert {r["id"] for r in b.execute("SELECT id FROM issues WHERE id LIKE 'RLS-%'")} == {"RLS-2"}
    with app_conn_for("") as none:
        assert none.execute("SELECT count(*) AS n FROM issues").fetchone()["n"] == 0
    conn.execute("DELETE FROM issues WHERE id LIKE 'RLS-%'")
    conn.commit()


def test_app_role_cannot_write_into_another_tenant(conn):
    import psycopg

    with app_conn_for("unitone") as a:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            a.execute("INSERT INTO issues (tenant_id, id, title) VALUES ('other','RLS-3','smuggled')")
        a.rollback()


def test_auth_lookup_functions_work_without_tenant(conn):
    conn.execute(
        """INSERT INTO users (id, tenant_id, email, name, google_sub) VALUES ('u_rls','unitone','rls@example.com','R','sub-rls')
           ON CONFLICT (tenant_id, email) DO UPDATE SET google_sub = EXCLUDED.google_sub"""
    )
    conn.commit()
    with app_conn_for("") as none:
        row = none.execute("SELECT * FROM auth_lookup_google(%s, %s)", ("sub-rls", "rls@example.com")).fetchone()
        assert row["tenant_id"] == "unitone" and row["user_id"] == "u_rls"
        assert none.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0
    conn.execute("DELETE FROM users WHERE id = 'u_rls'")
    conn.commit()


def test_every_tenant_table_has_forced_rls(conn):
    """Any table with a tenant_id column must be RLS-enabled + forced with the tenant_isolation policy."""
    tenant_tables = {
        r["table_name"]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='tenant_id'"
        )
    }
    assert "tenant_secrets" in tenant_tables
    forced = {
        r["relname"]
        for r in conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace='public'::regnamespace AND relrowsecurity AND relforcerowsecurity"
        )
    }
    policies = {
        r["tablename"] for r in conn.execute("SELECT tablename FROM pg_policies WHERE policyname='tenant_isolation'")
    }
    assert tenant_tables - forced == set(), f"tenant tables without forced RLS: {sorted(tenant_tables - forced)}"
    assert tenant_tables - policies == set(), f"tenant tables without policy: {sorted(tenant_tables - policies)}"
