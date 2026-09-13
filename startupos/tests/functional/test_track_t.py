"""Track T — multi-tenant runtime against Postgres, as the RLS-bound `startupos_app` role:

- ingest iterates active tenants × their non-disabled connections; credentials come from `tenant_secrets` through
  `settings.secret("kv:…", conn=, tenant_id=)`; the literal credential "fixture" reads tests/fixtures; tenant B's
  fixtures never land in tenant A's rows; a broken tenant records `connections.last_error` and stops nothing;
- the scheduler discovers tenants through `tenants_active()` and refresh_tenants picks up a sign-up;
- the Slack gateway resolves the tenant only through auth_lookup_slack;
- budgets are keyed by tenant with the allowance from `tenants.tier`.
"""

from __future__ import annotations

import base64
import os
from datetime import date

import pytest
from psycopg.types.json import Jsonb

from auth import bootstrap
from common import secrets
from common.tenants import active_tenants, list_active_tenants, tenant_conn
from daemon import budget, scheduler
from daemon.gateway import slack as gateway
from ingest import runner
from tests.conftest import APP_TEST_DSN

pytestmark = pytest.mark.functional

TA, TB, TC, TZ = "t-alpha", "t-beta", "t-gamma", "t-zeta"  # zeta is suspended
ALL = (TA, TB, TC, TZ)
TABLES = ("issues", "projects", "messages", "bills", "vendors", "cards", "accounts_bank", "transactions", "deployments")


def _key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


@pytest.fixture()
def master(monkeypatch):
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", _key())
    monkeypatch.delenv("STARTUPOS_DEV", raising=False)


def _wipe(conn):
    for t in (*TABLES, "events", "connections", "tenant_secrets", "budgets", "users", "sessions"):
        conn.execute(f"DELETE FROM {t} WHERE tenant_id = ANY(%s)", (list(ALL),))
    conn.commit()


@pytest.fixture()
def tenants(conn):
    """Four tenants on the superuser connection: alpha (brex fixture), beta (linear fixture), gamma (broken kv ref),
    zeta (suspended). Rows are cleaned before and after."""
    _wipe(conn)
    for t in ALL:
        conn.execute(
            "INSERT INTO tenants (id, name, status, tier) VALUES (%s, %s, 'active', 'founder') "
            "ON CONFLICT (id) DO UPDATE SET status = 'active', tier = 'founder', timezone = 'America/Los_Angeles'",
            (t, t),
        )
    conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (TZ,))
    conn.commit()
    yield
    _wipe(conn)
    conn.execute("UPDATE tenants SET status = 'active' WHERE id = ANY(%s)", (list(ALL),))
    conn.commit()


def _connect(tenant_id: str, source: str, credential: str | None, *, status: str = "connected") -> None:
    """What POST /onboarding/connections {credential} does (Track S), on a tenant-bound app connection."""
    ref = f"kv:{source}_api_key"
    with tenant_conn(tenant_id, APP_TEST_DSN) as c:
        if credential is not None:
            secrets.put(c, tenant_id, f"{source}_api_key", credential)
        c.execute(
            """INSERT INTO connections (id, tenant_id, source, secret_ref, config, status)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id, source) DO UPDATE SET secret_ref = EXCLUDED.secret_ref, status = EXCLUDED.status""",
            (f"{tenant_id}-{source}", tenant_id, source, ref, Jsonb({}), status),
        )


def counts(conn, tenant_id: str) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT count(*) AS n FROM {t} WHERE tenant_id = %s", (tenant_id,)).fetchone()["n"]
        for t in TABLES
    }


def connection(conn, tenant_id: str, source: str) -> dict:
    return dict(
        conn.execute("SELECT * FROM connections WHERE tenant_id=%s AND source=%s", (tenant_id, source)).fetchone()
    )


# --- active_tenants / tenants_active() ---------------------------------------------------------------------


def test_active_tenants_lists_only_active_rows_through_the_app_role(conn, tenants):
    listed = {t["id"] for t in list_active_tenants(APP_TEST_DSN)}
    assert {TA, TB, TC} <= listed and TZ not in listed
    row = next(t for t in active_tenants(conn) if t["id"] == TA)
    assert row["timezone"] == "America/Los_Angeles" and row["pulse_hour"] == 7 and row["status"] == "active"
    # the app role, bound to one tenant, still sees only its own row on the table itself
    with tenant_conn(TA, APP_TEST_DSN) as c:
        assert [r["id"] for r in c.execute("SELECT id FROM tenants")] == [TA]
        assert len(active_tenants(c)) >= 3


# --- two-tenant ingest isolation ------------------------------------------------------------------------------


def test_two_tenant_ingest_isolation(conn, tenants, master, monkeypatch):
    # no operator env keys anywhere: everything must come from tenant_secrets
    for var in ("LINEAR_API_KEY", "SLACK_BOT_TOKEN", "BREX_API_TOKEN", "VERCEL_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    _connect(TA, "brex", "fixture")
    _connect(TA, "linear", "fixture", status="disabled")  # disabled → never run
    _connect(TB, "linear", "fixture")
    _connect(TB, "vercel", "fixture")
    _connect(TC, "linear", None)  # a connection whose kv secret was never stored
    _connect(TZ, "linear", "fixture")  # suspended tenant: never iterated

    out = runner.run_tenants(dsn=APP_TEST_DSN)

    assert set(out) >= {TA, TB, TC} and TZ not in out
    assert set(out[TA]) == {"brex"} and set(out[TB]) == {"linear", "vercel"}  # skipped sources are absent
    assert out[TC] == {"linear": None}  # failed, isolated
    assert all(res is not None for res in (*out[TA].values(), *out[TB].values()))

    a, b, c, z = counts(conn, TA), counts(conn, TB), counts(conn, TC), counts(conn, TZ)
    assert a["bills"] == 3 and a["issues"] == 0 and a["deployments"] == 0
    assert b["issues"] == 9 and b["projects"] == 3 and b["deployments"] == 2 and b["bills"] == 0
    assert not any(c.values()) and not any(z.values())
    # every ingested row and event is stamped with its own tenant, and the same fixture ids never cross tenants
    assert (
        conn.execute("SELECT count(*) AS n FROM issues WHERE id = 'ACM-158' AND tenant_id = %s", (TB,)).fetchone()["n"]
        == 1
    )
    assert (
        conn.execute("SELECT count(*) AS n FROM issues WHERE id = 'ACM-158' AND tenant_id <> %s", (TB,)).fetchone()["n"]
        == 0
    )
    assert (
        conn.execute("SELECT count(*) AS n FROM events WHERE tenant_id = %s AND source = 'linear'", (TA,)).fetchone()[
            "n"
        ]
        == 0
    )

    # connections rows: success stamps last_sync_at and mode=fixtures, keeps the kv ref; failure records last_error
    ok = connection(conn, TB, "linear")
    assert ok["last_sync_at"] is not None and ok["last_error"] is None and ok["status"] == "connected"
    assert ok["secret_ref"] == "kv:linear_api_key" and ok["config"]["mode"] == "fixtures"
    bad = connection(conn, TC, "linear")
    assert bad["last_sync_at"] is None and bad["last_error"].startswith("MissingCredential: no credential for kv:")
    assert "fixture" not in bad["last_error"]
    assert connection(conn, TA, "linear")["last_sync_at"] is None  # disabled: untouched

    # idempotent second pass: no new events for either tenant
    events_before = conn.execute("SELECT count(*) AS n FROM events WHERE tenant_id = ANY(%s)", (list(ALL),)).fetchone()[
        "n"
    ]
    again = runner.run_tenants(dsn=APP_TEST_DSN)
    assert all(r.created == 0 and r.updated == 0 for r in again[TB]["linear"].values())
    assert (
        conn.execute("SELECT count(*) AS n FROM events WHERE tenant_id = ANY(%s)", (list(ALL),)).fetchone()["n"]
        == events_before
    )


def test_real_credential_is_passed_to_sync_and_never_written_on_error(conn, tenants, master, monkeypatch):
    from ingest import linear

    seen: dict = {}

    def fake_sync(c, tenant_id, *, api_key=None, use_fixtures=False, **kw):
        seen[tenant_id] = (api_key, use_fixtures)
        if tenant_id == TB:
            raise RuntimeError("linear graphql error: 401")
        return {"issues": linear.issues_upserter.upsert(c, tenant_id, [])}

    monkeypatch.setattr(linear, "sync", fake_sync)
    _connect(TA, "linear", "lin_api_AAA")
    _connect(TB, "linear", "lin_api_BBB")
    out = runner.run_tenants(["linear"], dsn=APP_TEST_DSN)
    assert seen[TA] == ("lin_api_AAA", False) and seen[TB] == ("lin_api_BBB", False)
    assert out[TA]["linear"] is not None and out[TB]["linear"] is None
    err = connection(conn, TB, "linear")["last_error"]
    assert err == "RuntimeError: linear graphql error: 401" and "lin_api" not in err
    assert connection(conn, TA, "linear")["last_error"] is None


def test_missing_master_key_is_a_per_tenant_error(conn, tenants, master, monkeypatch):
    _connect(TA, "linear", "fixture")
    monkeypatch.delenv("STARTUPOS_MASTER_KEY")
    out = runner.run_tenants(["linear"], dsn=APP_TEST_DSN)
    assert out[TA] == {"linear": None}
    assert connection(conn, TA, "linear")["last_error"].startswith("SecretsUnavailable")
    assert counts(conn, TA)["issues"] == 0


def test_dev_cli_path_still_bootstraps_from_env_and_fixtures(conn, tenants, monkeypatch):
    """`make ingest` / `--fixtures` on the default tenant: no connection rows needed, env refs bootstrap them."""
    for var in ("LINEAR_API_KEY", "SLACK_BOT_TOKEN", "BREX_API_TOKEN", "VERCEL_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with tenant_conn(TA, APP_TEST_DSN) as c:
        out = runner.run_all(c, TA, ["linear", "brex"])
        assert out == {"linear": {}, "brex": {}}  # skipped, disabled, no fixtures mixed in
        assert connection(c, TA, "linear")["status"] == "disabled"
        out = runner.run_all(c, TA, ["linear"], force_fixtures=True)
        assert out["linear"]["issues"].created == 9
        row = connection(c, TA, "linear")
        assert row["secret_ref"] == "env:LINEAR_API_KEY" and row["config"]["mode"] == "fixtures"
    assert counts(conn, TB)["issues"] == 0


# --- scheduler discovers tenants from the DB -----------------------------------------------------------------


def test_scheduler_discovers_tenants_and_refresh_picks_up_a_sign_up(conn, tenants):
    sched = scheduler.build_scheduler(dsn=APP_TEST_DSN)
    have = scheduler.scheduled_tenants(sched)
    assert {TA, TB, TC} <= have and TZ not in have
    assert f"morning_pulse:{TA}" in {j.id for j in sched.get_jobs()} and scheduler.REFRESH_TENANTS_ID in {
        j.id for j in sched.get_jobs()
    }
    # sign-up: a new active tenant appears → refresh adds its jobs; suspending one removes them
    conn.execute("INSERT INTO tenants (id, name, timezone, pulse_hour) VALUES ('t-new', 't-new', 'Europe/Berlin', 9)")
    conn.execute("UPDATE tenants SET status = 'suspended' WHERE id = %s", (TC,))
    conn.commit()
    try:
        out = scheduler.refresh_tenants(sched, dsn=APP_TEST_DSN)
        assert "t-new" in out["added"] and TC in out["removed"]
        job = sched.get_job("morning_pulse:t-new")
        assert "hour='9'" in str(job.trigger) and str(job.trigger.timezone) == "Europe/Berlin"
        assert sched.get_job(f"morning_pulse:{TC}") is None
    finally:
        conn.execute("DELETE FROM tenants WHERE id = 't-new'")
        conn.commit()


def test_scheduler_jobs_run_on_tenant_bound_connections(conn, tenants, monkeypatch):
    """tenant_cadence / run_signal_engine open get_conn(tenant_id=…): as the app role they see that tenant only."""
    from common.db import get_conn as real_get_conn

    conn.execute("UPDATE tenants SET pulse_hour = 5, timezone = 'Asia/Tokyo' WHERE id = %s", (TB,))
    conn.commit()
    monkeypatch.setattr(
        scheduler, "get_conn", lambda dsn=None, tenant_id=None: real_get_conn(APP_TEST_DSN, tenant_id=tenant_id)
    )
    assert scheduler.tenant_cadence(TB) == {"timezone": "Asia/Tokyo", "pulse_hour": 5, "pulse_channel": "web"}
    assert scheduler.tenant_cadence(TA)["pulse_hour"] == 7
    out = scheduler.run_signal_engine(TB)  # bound to TB: the analytics_off signal lands on TB's row only
    assert [s["id"] for s in out["signals"]] == ["marketing.analytics_off:posthog"]
    assert [
        r["tenant_id"] for r in conn.execute("SELECT tenant_id FROM signals WHERE tenant_id = ANY(%s)", (list(ALL),))
    ] == [TB]
    conn.execute("DELETE FROM signals WHERE tenant_id = ANY(%s)", (list(ALL),))
    conn.commit()


# --- gateway tenant resolution ----------------------------------------------------------------------------------


def test_gateway_resolves_tenant_from_slack_user_and_refuses_unknown(conn, tenants, monkeypatch):
    monkeypatch.delenv("STARTUPOS_DEV", raising=False)
    ua = bootstrap.upsert_owner(conn, TA, "a@alpha.test")
    ub = bootstrap.upsert_owner(conn, TB, "b@beta.test")
    conn.execute("UPDATE users SET slack_user_id = 'U_ALPHA' WHERE id = %s", (ua["id"],))
    conn.execute("UPDATE users SET slack_user_id = 'U_BETA' WHERE id = %s", (ub["id"],))
    conn.commit()
    resolver = lambda u: gateway.resolve_slack_user(u, APP_TEST_DSN)  # noqa: E731
    assert gateway.resolve_event_tenant("U_ALPHA", resolver=resolver) == (TA, ua["id"])
    assert gateway.resolve_event_tenant("U_BETA", resolver=resolver) == (TB, ub["id"])
    assert gateway.resolve_event_tenant("U_NOBODY", resolver=resolver) is None  # → link_hint(), no settings fallback
    # a disabled user is no longer a route into their tenant
    conn.execute("UPDATE users SET status = 'disabled' WHERE id = %s", (ub["id"],))
    conn.commit()
    assert gateway.resolve_event_tenant("U_BETA", resolver=resolver) is None
    # the resolved tenant is what handle_text sees: `pending` for beta never lists alpha's approvals
    conn.execute(
        """INSERT INTO approvals (id, tenant_id, module, type, target, preview, status)
           VALUES ('apr-alpha', %s, 'build', 'assign', 'ACM-1', 'x', 'pending')""",
        (TA,),
    )
    conn.commit()
    try:
        with tenant_conn(TA, APP_TEST_DSN) as c:
            assert "apr-alpha" in gateway.handle_text(c, TA, "pending")
        with tenant_conn(TB, APP_TEST_DSN) as c:
            assert gateway.handle_text(c, TB, "pending") == "Nothing waiting for you."
    finally:
        conn.execute("DELETE FROM approvals WHERE id = 'apr-alpha'")
        conn.commit()


class _FakeWeb:
    """A Slack WebClient stand-in that remembers which token it was built with."""

    def __init__(self, token, posted):
        self.token, self.posted = token, posted

    def chat_postMessage(self, **kw):
        self.posted.append({"token": self.token, **kw})

    def chat_postEphemeral(self, **kw):
        self.posted.append({"token": self.token, "ephemeral": True, **kw})


class _FakeSM:
    def send_socket_mode_response(self, response):
        pass


def _event(slack_user: str, text: str):
    class Req:
        type = "events_api"
        envelope_id = "env-1"
        payload = {"event": {"type": "app_mention", "user": slack_user, "channel": "C1", "text": text}}

    return Req()


def test_dev_gateway_replies_with_the_resolved_tenants_own_token(conn, tenants, master, monkeypatch):
    """PE follow-up (Sprint 3b): the Socket Mode listener holds ONE operator token.

    A reply for tenant B must post with B's own bot token, never the operator's; a tenant with no Slack
    credential (and that is not the install tenant) gets no reply at all rather than one sent as the operator.
    """
    monkeypatch.setenv("STARTUPOS_DEV", "1")
    monkeypatch.setattr(gateway, "_dsn", lambda: APP_TEST_DSN)
    ua = bootstrap.upsert_owner(conn, TA, "a@alpha.test")
    ub = bootstrap.upsert_owner(conn, TB, "b@beta.test")
    conn.execute("UPDATE users SET slack_user_id = 'U_ALPHA' WHERE id = %s", (ua["id"],))
    conn.execute("UPDATE users SET slack_user_id = 'U_BETA' WHERE id = %s", (ub["id"],))
    conn.commit()
    _connect(TB, "slack", None)  # the connections row…
    with tenant_conn(TB, APP_TEST_DSN) as c:  # …pointing at B's own encrypted bot token
        secrets.put(c, TB, "slack_api_key", "xoxb-beta")

    posted: list[dict] = []
    operator = _FakeWeb("xoxb-operator", posted)
    monkeypatch.setattr(gateway, "_new_web_client", lambda token: _FakeWeb(token, posted))
    on_request = gateway._listener(operator, "xoxb-operator", TA)

    on_request(_FakeSM(), _event("U_BETA", "pending"))
    assert [(p["token"], p["text"]) for p in posted] == [("xoxb-beta", "Nothing waiting for you.")]

    # alpha is mapped but has no Slack credential: the reply is dropped, not posted with the operator's token
    posted.clear()
    on_request(_FakeSM(), _event("U_ALPHA", "pending"))
    assert posted == []


# --- budgets per tenant -------------------------------------------------------------------------------------------


def test_budgets_are_per_tenant_with_the_allowance_from_the_tenant_tier(conn, tenants):
    conn.execute("UPDATE tenants SET tier = 'team' WHERE id = %s", (TB,))
    conn.commit()
    month = date(2026, 9, 1)
    with tenant_conn(TA, APP_TEST_DSN) as ca, tenant_conn(TB, APP_TEST_DSN) as cb:
        a = budget.get_or_create(ca, TA, month)
        b = budget.get_or_create(cb, TB, month)
        assert a["tier2_tokens_allowed"] == 1_500_000 and b["tier2_tokens_allowed"] == 5_000_000
        # push beta to conserve, then exhausted; alpha stays normal throughout
        b = budget.charge(cb, TB, 2, 4_600_000, 1.0, month)
        assert b["state"] == "conserve" and budget.current_state(ca, TA, month) == "normal"
        b = budget.charge(cb, TB, 2, 500_000, 1.0, month)
        assert b["state"] == "exhausted"
        assert budget.current_state(cb, TB, month) == "exhausted" and budget.current_state(ca, TA, month) == "normal"
        a = budget.charge(ca, TA, 2, 100, 0.01, month)
        assert a["tier2_tokens_used"] == 100 and a["state"] == "normal"
        # RLS: each tenant sees only its own budgets row
        assert [r["tenant_id"] for r in ca.execute("SELECT tenant_id FROM budgets")] == [TA]
        assert [r["tenant_id"] for r in cb.execute("SELECT tenant_id FROM budgets")] == [TB]


# --- signals and approvals are keyed per tenant -------------------------------------------------------------------


def test_signal_engine_and_approvals_do_not_collide_across_tenants(conn, tenants):
    """`marketing.analytics_off:posthog` fires for every tenant with the same id; so do approval ids derived
    from shared entity ids. Before Track T both were global primary keys and the second tenant's tick failed."""
    from signals import engine

    with tenant_conn(TA, APP_TEST_DSN) as ca, tenant_conn(TB, APP_TEST_DSN) as cb:
        a = engine.run(ca, TA)
        b = engine.run(cb, TB)
        assert a["fired"] >= 1 and b["fired"] == a["fired"]
        ids_a = {r["id"] for r in ca.execute("SELECT id FROM signals")}
        ids_b = {r["id"] for r in cb.execute("SELECT id FROM signals")}
        assert ids_a == ids_b and "marketing.analytics_off:posthog" in ids_a
        sid = "marketing.analytics_off:posthog"
        for c, t in ((ca, TA), (cb, TB)):
            c.execute(
                """INSERT INTO approvals (id, tenant_id, module, type, target, preview, status, signal_id)
                   VALUES ('apr-shared', %s, 'marketing', 'note', 'posthog', 'x', 'pending', %s)""",
                (t, sid),
            )
        assert [r["tenant_id"] for r in ca.execute("SELECT tenant_id FROM approvals WHERE id = 'apr-shared'")] == [TA]
        assert [r["tenant_id"] for r in cb.execute("SELECT tenant_id FROM approvals WHERE id = 'apr-shared'")] == [TB]
    rows = conn.execute("SELECT tenant_id FROM signals WHERE id = %s ORDER BY tenant_id", (sid,)).fetchall()
    assert [r["tenant_id"] for r in rows if r["tenant_id"] in (TA, TB)] == [TA, TB]
    conn.execute("DELETE FROM approvals WHERE tenant_id = ANY(%s)", (list(ALL),))
    conn.execute("DELETE FROM signals WHERE tenant_id = ANY(%s)", (list(ALL),))
    conn.commit()
