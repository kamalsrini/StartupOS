"""The founder's Slack channel, end to end: the install's channel is used until the founder overrides it.

The gap this closes: `tenants.slack_channel` took precedence over the install's `default_channel` but nothing
ever wrote it, so every tenant posted to the `#general` the column defaulted to. It is now NULLABLE with no
default ("unset" = use the install's channel), the legacy default is migrated away, and `POST /onboarding/cadence`
is where a founder sets it — which is what `web/app/settings` calls and what `GET /slack/status` reports.

Postgres-backed, no network: delivery's channel resolution is asked directly, the routes through TestClient.
"""

from __future__ import annotations

import base64
import os

import pytest
from fastapi.testclient import TestClient

from common.db import ensure_tenant
from daemon import delivery
from tests.conftest import login_as

pytestmark = pytest.mark.functional

T = "chan-alpha"
INSTALL_CHANNEL = "#founders"


def wipe(conn) -> None:
    for table in ("slack_installations", "sessions", "users", "budgets"):
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (T,))  # noqa: S608 - constant table names
    conn.execute("DELETE FROM tenants WHERE id = %s", (T,))
    conn.commit()


def seed_install(conn, channel: str = INSTALL_CHANNEL) -> None:
    conn.execute(
        """INSERT INTO slack_installations (tenant_id, team_id, team_name, bot_user_id, default_channel)
           VALUES (%s, 'T-CHAN', 'Chan Inc', 'U0BOT', %s)
           ON CONFLICT (tenant_id) DO UPDATE SET default_channel = EXCLUDED.default_channel, revoked_at = NULL""",
        (T, channel),
    )
    conn.commit()


@pytest.fixture()
def client(conn, auth_env, monkeypatch):
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    wipe(conn)
    ensure_tenant(conn, T, "Chan Inc", "https://chan.test")
    # Slack delivery on, and no override: exactly what a tenant looks like the moment it finishes installing.
    conn.execute("UPDATE tenants SET pulse_channel = 'slack', slack_channel = NULL WHERE id = %s", (T,))
    seed_install(conn)
    _, cookie = login_as(conn, T, "owner@chan.test", "Chan Owner")
    from api.main import app

    with TestClient(app, follow_redirects=False) as c:
        c.cookies.set("sos_session", cookie)
        yield c
    wipe(conn)


def channel_of(conn) -> str | None:
    return conn.execute("SELECT slack_channel FROM tenants WHERE id = %s", (T,)).fetchone()["slack_channel"]


def cadence(client: TestClient, **body) -> dict:
    r = client.post("/onboarding/cadence", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# --- the install's channel is the default ---------------------------------------------------------


def test_an_installed_tenant_with_no_override_posts_to_the_install_channel(client, conn):
    conn.commit()
    assert channel_of(conn) is None  # unset, not '#general'
    assert delivery.resolve_channel(conn, T)["channel"] == INSTALL_CHANNEL

    status = client.get("/slack/status").json()
    assert status["slack_channel"] is None  # no override
    assert status["default_channel"] == INSTALL_CHANNEL
    assert status["channel"] == INSTALL_CHANNEL  # the channel actually in effect
    assert status["pulse_channel"] == "slack"


# --- the override ---------------------------------------------------------------------------------


def test_the_founders_override_wins_over_the_install_channel(client, conn):
    out = cadence(client, slack_channel="#Ops")
    assert out["slack_channel"] == "#ops"  # lower-cased, the way Slack names channels

    conn.commit()
    assert channel_of(conn) == "#ops"
    assert delivery.resolve_channel(conn, T)["channel"] == "#ops"
    status = client.get("/slack/status").json()
    assert status["slack_channel"] == "#ops" and status["channel"] == "#ops"
    assert status["default_channel"] == INSTALL_CHANNEL  # the install is untouched, it is just outranked


def test_a_channel_id_is_accepted(client, conn):
    assert cadence(client, slack_channel="C0123ABCD")["slack_channel"] == "C0123ABCD"
    conn.commit()
    assert delivery.resolve_channel(conn, T)["channel"] == "C0123ABCD"


@pytest.mark.parametrize("bad", ["general", "#with space", "#!", "##ops", "C0", "kv:slack_bot_token", "#" + "x" * 90])
def test_a_bad_channel_is_refused_and_nothing_is_written(client, conn, bad):
    cadence(client, slack_channel="#ops")
    r = client.post("/onboarding/cadence", json={"slack_channel": bad})
    assert r.status_code == 422 and "channel" in r.json()["detail"]
    conn.commit()
    assert channel_of(conn) == "#ops"  # the previous choice still stands


def test_clearing_the_override_falls_back_to_the_install_channel(client, conn):
    cadence(client, slack_channel="#ops")
    assert cadence(client, slack_channel="")["slack_channel"] is None
    conn.commit()
    assert channel_of(conn) is None
    assert delivery.resolve_channel(conn, T)["channel"] == INSTALL_CHANNEL
    assert client.get("/slack/status").json()["channel"] == INSTALL_CHANNEL


def test_setting_the_channel_leaves_the_rest_of_the_cadence_alone(client, conn):
    cadence(client, timezone="Asia/Tokyo", pulse_hour=5, channel="both", tier2_tokens_allowed=42_000)
    out = cadence(client, slack_channel="#ops")
    assert (out["timezone"], out["pulse_hour"], out["channel"]) == ("Asia/Tokyo", 5, "both")
    assert out["tier2_tokens_allowed"] == 42_000
    conn.commit()
    row = conn.execute("SELECT timezone, pulse_hour, pulse_channel FROM tenants WHERE id = %s", (T,)).fetchone()
    assert dict(row) == {"timezone": "Asia/Tokyo", "pulse_hour": 5, "pulse_channel": "both"}


def test_an_uninstalled_tenant_with_an_override_still_resolves_its_channel(client, conn):
    """The override is the tenant's, not the install's: revoking Slack does not silently change where posts go."""
    cadence(client, slack_channel="#ops")
    conn.execute("UPDATE slack_installations SET revoked_at = now() WHERE tenant_id = %s", (T,))
    conn.commit()
    assert delivery.install_channel(conn, T) is None
    assert delivery.resolve_channel(conn, T)["channel"] == "#ops"


# --- the migration --------------------------------------------------------------------------------


def test_the_migration_turns_a_legacy_general_row_into_unset(client, conn, test_dsn):
    """Re-create the pre-fix column (NOT NULL DEFAULT '#general'), re-apply the schema, and check both halves:
    the untouched default becomes NULL, and delivery then resolves the install's channel instead of '#general'."""
    from common.db import apply_schema

    conn.execute("UPDATE tenants SET slack_channel = '#general' WHERE slack_channel IS NULL")
    conn.execute("ALTER TABLE tenants ALTER COLUMN slack_channel SET NOT NULL")
    conn.execute("ALTER TABLE tenants ALTER COLUMN slack_channel SET DEFAULT '#general'")
    conn.commit()
    assert channel_of(conn) == "#general"
    assert delivery.resolve_channel(conn, T)["channel"] == "#general"  # the bug: the install is overridden

    conn.commit()  # release this connection's lock on `tenants`, or the migration's DDL waits for it
    apply_schema(test_dsn)

    conn.commit()  # a fresh snapshot after the migration's DDL
    assert channel_of(conn) is None
    assert delivery.resolve_channel(conn, T)["channel"] == INSTALL_CHANNEL
    assert client.get("/slack/status").json()["channel"] == INSTALL_CHANNEL

    col = conn.execute(
        """SELECT is_nullable, column_default FROM information_schema.columns
             WHERE table_schema = 'public' AND table_name = 'tenants' AND column_name = 'slack_channel'"""
    ).fetchone()
    assert col["is_nullable"] == "YES" and col["column_default"] is None

    # …and applying it again changes nothing (a founder's '#general' set after the migration is NOT touched)
    conn.execute("UPDATE tenants SET slack_channel = '#general' WHERE id = %s", (T,))
    conn.commit()
    apply_schema(test_dsn)
    conn.commit()
    assert channel_of(conn) == "#general"
