"""Track I — Slack install and inbound HTTP against Postgres (CONTRACTS.md Sprint 3b, Track I).

Acceptance: a tenant installs the StartupOS Slack app into its OWN workspace over OAuth with no operator touching
`.env`; the bot token lands encrypted in `tenant_secrets` (never in `slack_installations`, never in a response,
never in a log); inbound events authenticate with Slack's signature and resolve their tenant only from `team_id`;
uninstall revokes the install, the token and the connection; and one workspace belongs to exactly one company.

No network: the OAuth exchange, the Slack post and the clock are all injected.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from auth import slack_install, slack_sig
from common import jobs as queue
from common import secrets
from tests.conftest import APP_TEST_DSN, login_as

pytestmark = pytest.mark.functional

TA = "slack-alpha"  # first company, workspace T-ALPHA
TB = "slack-beta"  # second company, workspace T-BETA
SIGNING_SECRET = "test-slack-signing-secret"  # noqa: S105 - test fixture
TOKEN_A = "xoxb-alpha-0000-token"  # noqa: S105 - test fixture
TOKEN_B = "xoxb-beta-1111-token"  # noqa: S105 - test fixture
NOW = int(time.time())  # the replay window is real: sign with a real clock

TABLES = ("tenant_jobs", "connections", "tenant_secrets", "slack_installations", "sessions", "users")


def oauth_response(team_id: str, team_name: str, token: str, bot_user_id: str = "U0BOT") -> dict[str, Any]:
    """The shape Slack's oauth.v2.access actually returns for a bot install."""
    return {
        "ok": True,
        "access_token": token,
        "token_type": "bot",
        "scope": ",".join(slack_install.SCOPES),
        "bot_user_id": bot_user_id,
        "app_id": "A0STARTUPOS",
        "team": {"id": team_id, "name": team_name},
        "enterprise": None,
        "authed_user": {"id": "U0HUMAN"},
    }


class FakeOAuth:
    """Stands in for httpx: records the exchange and answers with a canned oauth.v2.access body."""

    def __init__(self, body: dict[str, Any], status: int = 200) -> None:
        self.body, self.status, self.calls = body, status, []

    def post(self, url: str, data: dict[str, Any] | None = None, **_: Any) -> Any:
        self.calls.append((url, dict(data or {})))
        return SimpleNamespace(status_code=self.status, json=lambda: self.body)


def our_logs(caplog) -> str:
    """Only StartupOS log records — httpx's own test-client request log echoes the URL we are asserting about."""
    return "\n".join(
        r.getMessage() for r in caplog.records if r.name.split(".")[0] in ("api", "auth", "daemon", "common", "root")
    )


def _master_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def wipe(conn, tenant_id: str) -> None:
    for t in TABLES:
        conn.execute(f"DELETE FROM {t} WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
    conn.commit()


@pytest.fixture()
def slack_env(monkeypatch, auth_env):
    monkeypatch.setenv("STARTUPOS_SLACK_CLIENT_ID", "1111.2222")
    monkeypatch.setenv("STARTUPOS_SLACK_CLIENT_SECRET", "slack-client-secret")
    monkeypatch.setenv("STARTUPOS_SLACK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", _master_key())
    monkeypatch.setenv("STARTUPOS_WEB_URL", "https://app.startupos.test")
    monkeypatch.setenv("STARTUPOS_PUBLIC_URL", "https://api.startupos.test")
    return auth_env


@pytest.fixture()
def client(conn, slack_env):
    from common.db import ensure_tenant

    for t in (TA, TB):
        wipe(conn, t)
        ensure_tenant(conn, t, t.title(), f"https://{t}.test")
    conn.execute("DELETE FROM tenant_jobs WHERE status = ANY(%s)", (list(queue.ACTIVE),))
    conn.commit()
    user_a, cookie_a = login_as(conn, TA, "owner@alpha.test", "Alpha Owner")
    user_b, cookie_b = login_as(conn, TB, "owner@beta.test", "Beta Owner")
    from api.main import app

    with TestClient(app, follow_redirects=False) as c:
        c.users = {TA: user_a, TB: user_b}
        c.cookies_for = {TA: cookie_a, TB: cookie_b}
        yield c
    for t in (TA, TB):
        wipe(conn, t)


def as_tenant(client: TestClient, tenant_id: str) -> TestClient:
    client.cookies.clear()
    client.cookies.set("sos_session", client.cookies_for[tenant_id])
    return client


def signed(body: bytes, *, secret: str = SIGNING_SECRET, ts: int = NOW, content_type: str = "application/json"):
    return {
        "X-Slack-Request-Timestamp": str(ts),
        "X-Slack-Signature": slack_sig.sign(ts, body, secret),
        "Content-Type": content_type,
    }


def post_event(client: TestClient, body: dict[str, Any], **kw: Any):
    raw = json.dumps(body).encode()
    return client.post("/slack/events", content=raw, headers=signed(raw, **kw))


def install_tenant(client, monkeypatch, tenant_id: str, team_id: str, team_name: str, token: str) -> Any:
    """Run the whole flow for one tenant with a faked exchange. Returns the callback response."""
    fake = FakeOAuth(oauth_response(team_id, team_name, token))
    monkeypatch.setattr(slack_install, "_http", fake)
    r = as_tenant(client, tenant_id).get("/slack/install")
    assert r.status_code == 302, r.text
    state = r.headers["location"].split("state=")[1].split("&")[0]
    return client.get("/slack/oauth/callback", params={"code": "fake-code-abc", "state": state}), fake


# --- state token ------------------------------------------------------------------------------------------


def test_state_token_round_trips_and_expires(slack_env):
    state = slack_install.make_state(TA, "user-1")
    assert slack_install.read_state(state) == {"tenant_id": TA, "user_id": "user-1"}
    assert slack_install.read_state(None) is None
    assert slack_install.read_state("") is None
    assert slack_install.read_state(state + "x") is None  # tampered
    assert slack_install.read_state(state[:-4]) is None
    assert slack_install.read_state(state, max_age=-1) is None  # expiry is enforced
    assert slack_install.make_state(TA) != slack_install.make_state(TA)  # nonce, so no replayable link


def test_state_from_another_install_secret_is_refused(slack_env, monkeypatch):
    state = slack_install.make_state(TA)
    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", "a-different-session-secret")
    assert slack_install.read_state(state) is None


# --- GET /slack/install -----------------------------------------------------------------------------------


def test_install_requires_auth_and_redirects_with_the_contract_scopes(client):
    client.cookies.clear()
    assert client.get("/slack/install").status_code == 401

    r = as_tenant(client, TA).get("/slack/install")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://slack.com/oauth/v2/authorize?")
    assert "client_id=1111.2222" in loc
    assert "redirect_uri=https%3A%2F%2Fapi.startupos.test%2Fslack%2Foauth%2Fcallback" in loc
    for scope in (
        "chat:write",
        "chat:write.public",
        "commands",
        "im:history",
        "app_mentions:read",
        "users:read",
        "channels:read",
    ):
        assert scope.replace(":", "%3A") in loc
    state = loc.split("state=")[1].split("&")[0]
    assert slack_install.read_state(state)["tenant_id"] == TA


def test_install_without_env_is_503_not_a_broken_redirect(client, monkeypatch):
    monkeypatch.delenv("STARTUPOS_SLACK_CLIENT_ID", raising=False)
    r = as_tenant(client, TA).get("/slack/install")
    assert r.status_code == 503 and "STARTUPOS_SLACK_CLIENT_ID" in r.text


# --- the OAuth callback -----------------------------------------------------------------------------------


def test_full_install_writes_installation_secret_and_connection_and_never_leaks_the_token(
    client, conn, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    r, fake = install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    assert r.status_code == 302
    assert r.headers["location"] == "https://app.startupos.test/settings?slack=connected"

    # the exchange used the operator's app credentials and our own redirect_uri
    url, data = fake.calls[0]
    assert url == "https://slack.com/api/oauth.v2.access"
    assert data["code"] == "fake-code-abc" and data["client_id"] == "1111.2222"
    assert data["redirect_uri"] == "https://api.startupos.test/slack/oauth/callback"

    row = conn.execute("SELECT * FROM slack_installations WHERE tenant_id = %s", (TA,)).fetchone()
    assert row["team_id"] == "T-ALPHA" and row["team_name"] == "Alpha Inc"
    assert row["bot_user_id"] == "U0BOT" and row["default_channel"] == "#general"
    assert row["revoked_at"] is None and row["installed_by"] == client.users[TA]
    assert TOKEN_A not in json.dumps(dict(row), default=str)  # the table holds no token, by design

    assert secrets.get(conn, TA, "slack_bot_token") == TOKEN_A  # only here, encrypted at rest
    ct = conn.execute(
        "SELECT ciphertext FROM tenant_secrets WHERE tenant_id = %s AND name = 'slack_bot_token'", (TA,)
    ).fetchone()["ciphertext"]
    assert TOKEN_A.encode() not in bytes(ct)

    cx = conn.execute("SELECT * FROM connections WHERE tenant_id = %s AND source = 'slack'", (TA,)).fetchone()
    assert cx["secret_ref"] == "kv:slack_bot_token" and cx["status"] == "connected"
    assert cx["config"]["team_id"] == "T-ALPHA"
    # what Track D and the executors resolve, unchanged
    assert secrets.credential_for_source(conn, TA, "slack") == TOKEN_A

    # nothing echoed the token: not the redirect, not the status endpoint, not a log line
    assert TOKEN_A not in r.text and TOKEN_A not in r.headers["location"]
    status = as_tenant(client, TA).get("/slack/status").json()
    assert status["connected"] is True and status["team_name"] == "Alpha Inc"
    logs = our_logs(caplog)
    assert TOKEN_A not in json.dumps(status) and "fake-code-abc" not in logs
    assert TOKEN_A not in logs and "slack-client-secret" not in logs


def test_callback_rejects_a_bad_or_expired_state_without_touching_the_database(client, conn, monkeypatch):
    monkeypatch.setattr(slack_install, "_http", FakeOAuth(oauth_response("T-ALPHA", "Alpha", TOKEN_A)))
    for params in (
        {"code": "c"},  # no state
        {"code": "c", "state": "not-a-token"},
        {"code": "c", "state": slack_install.make_state(TA) + "tamper"},
        {"state": slack_install.make_state(TA)},  # no code
    ):
        r = client.get("/slack/oauth/callback", params=params)
        assert r.status_code == 302 and "slack=error&reason=state" in r.headers["location"]
    assert not conn.execute("SELECT 1 FROM slack_installations").fetchone()

    r = client.get("/slack/oauth/callback", params={"error": "access_denied", "state": "x"})
    assert "reason=denied" in r.headers["location"]


def test_callback_reports_a_refused_exchange_without_leaking_the_code(client, conn, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(slack_install, "_http", FakeOAuth({"ok": False, "error": "invalid_code"}))
    state = slack_install.make_state(TA)
    r = client.get("/slack/oauth/callback", params={"code": "secret-code", "state": state})
    assert r.status_code == 302 and "slack=error&reason=exchange" in r.headers["location"]
    assert not conn.execute("SELECT 1 FROM slack_installations WHERE tenant_id = %s", (TA,)).fetchone()
    assert "secret-code" not in our_logs(caplog)


def test_two_tenants_install_into_their_own_workspaces(client, conn, monkeypatch):
    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    r, _ = install_tenant(client, monkeypatch, TB, "T-BETA", "Beta LLC", TOKEN_B)
    assert "slack=connected" in r.headers["location"]

    rows = {
        r["tenant_id"]: r["team_id"]
        for r in conn.execute("SELECT tenant_id, team_id FROM slack_installations").fetchall()
    }
    assert rows == {TA: "T-ALPHA", TB: "T-BETA"}
    assert secrets.get(conn, TA, "slack_bot_token") == TOKEN_A
    assert secrets.get(conn, TB, "slack_bot_token") == TOKEN_B
    assert secrets.credential_for_source(conn, TA, "slack") != secrets.credential_for_source(conn, TB, "slack")

    # each workspace resolves to its own tenant, and only to it
    from auth.slack_install import lookup_team

    assert lookup_team(conn, "T-ALPHA")["tenant_id"] == TA
    assert lookup_team(conn, "T-BETA")["tenant_id"] == TB
    assert lookup_team(conn, "T-NOBODY") is None


def test_one_workspace_cannot_be_installed_for_two_tenants(client, conn, monkeypatch):
    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    r, _ = install_tenant(client, monkeypatch, TB, "T-ALPHA", "Alpha Inc", TOKEN_B)
    assert r.status_code == 302 and "slack=error&reason=workspace_taken" in r.headers["location"]

    # tenant A keeps the workspace and its token; tenant B got no installation at all
    assert conn.execute("SELECT count(*) AS n FROM slack_installations").fetchone()["n"] == 1
    assert conn.execute("SELECT tenant_id FROM slack_installations").fetchone()["tenant_id"] == TA
    assert secrets.get(conn, TA, "slack_bot_token") == TOKEN_A
    assert not conn.execute("SELECT 1 FROM slack_installations WHERE tenant_id = %s", (TB,)).fetchone()
    assert as_tenant(client, TB).get("/slack/status").json()["connected"] is False


def test_reinstalling_the_same_workspace_for_the_same_tenant_rotates_the_token(client, conn, monkeypatch):
    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    r, _ = install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Renamed", "xoxb-alpha-rotated")
    assert "slack=connected" in r.headers["location"]
    row = conn.execute("SELECT * FROM slack_installations WHERE tenant_id = %s", (TA,)).fetchone()
    assert row["team_name"] == "Alpha Renamed"
    assert secrets.get(conn, TA, "slack_bot_token") == "xoxb-alpha-rotated"
    assert conn.execute("SELECT count(*) AS n FROM slack_installations").fetchone()["n"] == 1


# --- POST /slack/events -----------------------------------------------------------------------------------


def test_url_verification_challenge_is_echoed(client):
    body = {"type": "url_verification", "token": "z26uFbvR", "challenge": "3eZbrw1a"}
    r = post_event(client, body, ts=NOW)
    assert r.status_code == 200 and r.json() == {"challenge": "3eZbrw1a"}


def test_events_reject_a_bad_signature_a_stale_timestamp_and_a_missing_header(client):
    body = {"type": "url_verification", "challenge": "c"}
    raw = json.dumps(body).encode()

    assert client.post("/slack/events", content=raw).status_code == 403  # no headers at all
    assert client.post("/slack/events", content=raw, headers=signed(raw, secret="wrong")).status_code == 403
    # a valid signature over a different body (tampering in flight)
    other = json.dumps({"type": "url_verification", "challenge": "attacker"}).encode()
    assert client.post("/slack/events", content=other, headers=signed(raw)).status_code == 403
    # a correctly signed request from 10 minutes ago (replay)
    stale = client.post("/slack/events", content=raw, headers=signed(raw, ts=NOW - 600))
    assert stale.status_code == 403


def test_events_are_503_when_the_signing_secret_is_missing(client, monkeypatch):
    monkeypatch.delenv("STARTUPOS_SLACK_SIGNING_SECRET", raising=False)
    body = {"type": "url_verification", "challenge": "c"}
    raw = json.dumps(body).encode()
    r = client.post("/slack/events", content=raw, headers=signed(raw))
    assert r.status_code == 503 and "not configured" in r.text


def test_unknown_workspace_is_a_clean_200(client, conn):
    r = post_event(
        client,
        {
            "type": "event_callback",
            "team_id": "T-NOBODY",
            "event_id": "Ev1",
            "event": {"type": "app_mention", "user": "U1", "channel": "C1", "text": "hi"},
        },
    )
    assert r.status_code == 200  # never a 4xx: Slack would retry forever
    assert r.json()["response_type"] == "ephemeral" and "not connected" in r.json()["text"]
    assert not conn.execute("SELECT 1 FROM tenant_jobs WHERE kind = 'slack_event'").fetchone()


def test_a_mention_queues_one_slack_event_job_for_the_right_tenant(client, conn, monkeypatch):
    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    install_tenant(client, monkeypatch, TB, "T-BETA", "Beta LLC", TOKEN_B)

    r = post_event(
        client,
        {
            "type": "event_callback",
            "team_id": "T-BETA",
            "event_id": "Ev-100",
            "event": {
                "type": "app_mention",
                "user": "U0HUMAN",
                "channel": "C-BETA",
                "thread_ts": "1725400000.000100",
                "text": "<@U0BOT> pending",
            },
        },
    )
    assert r.status_code == 200 and r.json() == {"ok": True, "queued": True}
    rows = conn.execute("SELECT * FROM tenant_jobs WHERE kind = 'slack_event'").fetchall()
    assert len(rows) == 1
    job = rows[0]
    assert job["tenant_id"] == TB  # resolved from team_id alone
    assert job["payload"]["channel"] == "C-BETA" and job["payload"]["text"] == "<@U0BOT> pending"
    assert job["payload"]["thread_ts"] == "1725400000.000100"

    # Slack retries the same event_id when it thinks we were slow — we must not answer twice
    again = post_event(
        client,
        {
            "type": "event_callback",
            "team_id": "T-BETA",
            "event_id": "Ev-100",
            "event": {"type": "app_mention", "user": "U0HUMAN", "channel": "C-BETA", "text": "<@U0BOT> pending"},
        },
    )
    assert again.json() == {"ok": True, "queued": False}
    assert conn.execute("SELECT count(*) AS n FROM tenant_jobs WHERE kind = 'slack_event'").fetchone()["n"] == 1


@pytest.mark.parametrize(
    "event",
    [
        {"type": "app_mention", "user": "U0BOT", "channel": "C1", "text": "hi"},  # the bot's own message
        {"type": "message", "user": "U1", "channel": "C1", "text": "hi", "bot_id": "B1"},
        {"type": "message", "user": "U1", "channel": "C1", "text": "edited", "subtype": "message_changed"},
        {"type": "message", "user": "U1", "channel": "C1", "text": "   "},
        {"type": "reaction_added", "user": "U1", "item": {}},
    ],
)
def test_noise_is_acknowledged_and_dropped(client, conn, monkeypatch, event):
    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    r = post_event(client, {"type": "event_callback", "team_id": "T-ALPHA", "event_id": "Ev-x", "event": event})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert not conn.execute("SELECT 1 FROM tenant_jobs WHERE kind = 'slack_event'").fetchone()


def test_uninstall_revokes_the_install_the_token_and_the_connection(client, conn, monkeypatch):
    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    r = post_event(
        client,
        {"type": "event_callback", "team_id": "T-ALPHA", "event_id": "Ev-u", "event": {"type": "app_uninstalled"}},
    )
    assert r.status_code == 200 and r.json()["handled"] == "app_uninstalled"

    row = conn.execute("SELECT * FROM slack_installations WHERE tenant_id = %s", (TA,)).fetchone()
    assert row["revoked_at"] is not None
    assert secrets.get(conn, TA, "slack_bot_token") is None
    assert (
        conn.execute("SELECT status FROM connections WHERE tenant_id = %s AND source='slack'", (TA,)).fetchone()[
            "status"
        ]
        == "disabled"
    )
    assert secrets.credential_for_source(conn, TA, "slack") is None

    # a revoked workspace is "not connected" for every later event, and a repeat uninstall is still a clean 200
    ev = post_event(
        client,
        {
            "type": "event_callback",
            "team_id": "T-ALPHA",
            "event_id": "Ev-after",
            "event": {"type": "app_mention", "user": "U1", "channel": "C1", "text": "hi"},
        },
    )
    assert ev.status_code == 200 and "not connected" in ev.json()["text"]
    assert not conn.execute("SELECT 1 FROM tenant_jobs WHERE kind = 'slack_event'").fetchone()
    assert (
        post_event(
            client,
            {"type": "event_callback", "team_id": "T-ALPHA", "event_id": "Ev-u2", "event": {"type": "tokens_revoked"}},
        ).status_code
        == 200
    )
    assert as_tenant(client, TA).get("/slack/status").json()["connected"] is False


def test_tokens_revoked_for_an_unknown_workspace_is_a_clean_200(client):
    r = post_event(
        client,
        {"type": "event_callback", "team_id": "T-NOBODY", "event_id": "Ev-z", "event": {"type": "tokens_revoked"}},
    )
    assert r.status_code == 200


# --- the daemon side of an event ---------------------------------------------------------------------------


def test_the_daemon_answers_the_queued_event_with_that_tenants_own_token(client, conn, monkeypatch):
    from daemon import jobs
    from daemon.executors import slack as slack_exec

    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    conn.execute("UPDATE users SET slack_user_id = 'U0HUMAN' WHERE tenant_id = %s", (TA,))
    conn.commit()

    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        slack_exec, "post_message", lambda inp, *, client=None, token=None: posted.append({**inp, "token": token})
    )
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (0, 0))

    post_event(
        client,
        {
            "type": "event_callback",
            "team_id": "T-ALPHA",
            "event_id": "Ev-200",
            "event": {"type": "app_mention", "user": "U0HUMAN", "channel": "C-ALPHA", "text": "<@U0BOT> pending"},
        },
    )
    out = jobs.drain(tenants=[TA], dsn=APP_TEST_DSN)
    assert out["done"] == 1 and out["failed"] == 0, out
    assert len(posted) == 1
    assert posted[0]["channel"] == "C-ALPHA" and posted[0]["token"] == TOKEN_A
    assert "Nothing waiting for you" in posted[0]["text"]  # the existing gateway answered `pending`

    job = conn.execute("SELECT * FROM tenant_jobs WHERE kind='slack_event'").fetchone()
    assert job["status"] == "done" and job["payload"]["result"]["replied"] is True


def test_an_unlinked_slack_user_gets_the_link_hint_and_decides_nothing(client, conn, monkeypatch):
    from daemon import jobs
    from daemon.executors import slack as slack_exec

    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    # tenant B's owner is linked to this Slack id — a different company; the workspace is A's
    conn.execute("UPDATE users SET slack_user_id = 'U0STRANGER' WHERE tenant_id = %s", (TB,))
    conn.commit()

    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        slack_exec, "post_message", lambda inp, *, client=None, token=None: posted.append({**inp, "token": token})
    )
    post_event(
        client,
        {
            "type": "event_callback",
            "team_id": "T-ALPHA",
            "event_id": "Ev-300",
            "event": {"type": "app_mention", "user": "U0STRANGER", "channel": "C-ALPHA", "text": "<@U0BOT> pending"},
        },
    )
    assert jobs.drain(tenants=[TA], dsn=APP_TEST_DSN)["done"] == 1
    assert len(posted) == 1 and "Link your StartupOS account" in posted[0]["text"]
    assert posted[0]["token"] == TOKEN_A


# --- POST /slack/interactivity -----------------------------------------------------------------------------


def interactivity_body(payload: dict[str, Any]) -> bytes:
    from urllib.parse import urlencode

    return urlencode({"payload": json.dumps(payload)}).encode()


def test_interactivity_verifies_the_signature_and_404s_on_an_unknown_action(client, monkeypatch):
    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    payload = {
        "type": "block_actions",
        "team": {"id": "T-ALPHA"},
        "user": {"id": "U0HUMAN"},
        # Track B owns approval_approve/approval_decline; an action_id nobody handles is still a 404.
        "actions": [{"action_id": "cockpit_snooze", "value": "assign-uni-158"}],
    }
    raw = interactivity_body(payload)
    ct = "application/x-www-form-urlencoded"

    assert client.post("/slack/interactivity", content=raw).status_code == 403
    assert (
        client.post("/slack/interactivity", content=raw, headers=signed(raw, secret="no", content_type=ct)).status_code
        == 403
    )

    r = client.post("/slack/interactivity", content=raw, headers=signed(raw, content_type=ct))
    assert r.status_code == 404 and r.json()["detail"] == "unknown action"


def test_interactivity_turns_an_unexpected_handler_failure_into_an_ephemeral(client, monkeypatch, caplog):
    """PE follow-up (Sprint 3b): a 5xx makes Slack retry and shows the presser a red error.

    Anything the handler raises other than UnknownAction is logged once with the action id and the team, and
    answered 200 + ephemeral so the press ends there and the founder is told where the approval still is.
    """
    from daemon import slack_actions

    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)

    def boom(payload, *, conn_factory):
        raise RuntimeError("chat.update exploded")

    monkeypatch.setattr(slack_actions, "handle_action", boom)
    payload = {
        "type": "block_actions",
        "team": {"id": "T-ALPHA"},
        "user": {"id": "U0HUMAN"},
        "actions": [{"action_id": "approval_approve", "value": "apr-1"}],
    }
    raw = interactivity_body(payload)
    with caplog.at_level("ERROR", logger="api.slack"):
        r = client.post(
            "/slack/interactivity", content=raw, headers=signed(raw, content_type="application/x-www-form-urlencoded")
        )
    assert r.status_code == 200
    body = r.json()
    assert body["response_type"] == "ephemeral" and "cockpit still has this approval" in body["text"]
    logged = [rec for rec in caplog.records if rec.name == "api.slack"]
    assert len(logged) == 1 and "approval_approve" in logged[0].getMessage() and "T-ALPHA" in logged[0].getMessage()
    # UnknownAction is untouched: it still 404s
    monkeypatch.setattr(slack_actions, "handle_action", _raise_unknown)
    r = client.post(
        "/slack/interactivity", content=raw, headers=signed(raw, content_type="application/x-www-form-urlencoded")
    )
    assert r.status_code == 404


def _raise_unknown(payload, *, conn_factory):
    from daemon import slack_actions

    raise slack_actions.UnknownAction("nobody owns this")


def test_interactivity_from_an_unknown_workspace_is_a_clean_200(client):
    raw = interactivity_body({"type": "block_actions", "team": {"id": "T-NOBODY"}, "actions": []})
    r = client.post(
        "/slack/interactivity", content=raw, headers=signed(raw, content_type="application/x-www-form-urlencoded")
    )
    assert r.status_code == 200 and "not connected" in r.json()["text"]


# --- the auth fence ----------------------------------------------------------------------------------------


def _all_routes(routes: Any) -> Any:
    """FastAPI wraps an included router in a proxy object; walk into it so no route hides from the fence."""
    for route in routes:
        sub = getattr(route, "routes", None) or getattr(getattr(route, "original_router", None), "routes", None)
        if sub:
            yield from _all_routes(sub)
        else:
            yield route


def test_only_the_enumerated_routes_are_public(client):
    """Every route not in api.main.PUBLIC_PATHS must answer 401 without a principal (the Sprint 2 property)."""
    from api.main import PUBLIC_PATHS, app

    client.cookies.clear()
    checked = 0
    for route in _all_routes(app.routes):
        path, methods = getattr(route, "path", None), getattr(route, "methods", None)
        if not path or not methods or path in PUBLIC_PATHS:
            continue
        method = sorted(set(methods) - {"HEAD", "OPTIONS"})[0]
        url = re.sub(r"\{name\}", "build", path)
        url = re.sub(r"\{[^}]+\}", "x", url)
        r = client.request(method, url, json={} if method in ("POST", "PUT") else None)
        assert r.status_code == 401, f"{method} {url} answered {r.status_code}, not 401"
        assert r.headers.get("www-authenticate") == "Bearer", f"{method} {url} has no Bearer challenge"
        checked += 1
    assert checked >= 15, f"only {checked} routes behind the fence — did the walker miss a router?"
    assert {"/slack/events", "/slack/interactivity", "/slack/oauth/callback"} <= PUBLIC_PATHS
    assert "/slack/install" not in PUBLIC_PATHS and "/slack/status" not in PUBLIC_PATHS


# --- isolation ---------------------------------------------------------------------------------------------


def test_one_tenant_cannot_read_anothers_installation_or_token(client, monkeypatch):
    """RLS, not application code, is what keeps the workspaces apart under the `startupos_app` role."""
    from tests.conftest import app_conn_for

    install_tenant(client, monkeypatch, TA, "T-ALPHA", "Alpha Inc", TOKEN_A)
    install_tenant(client, monkeypatch, TB, "T-BETA", "Beta LLC", TOKEN_B)

    with app_conn_for(TB) as b:
        assert b.execute("SELECT count(*) AS n FROM slack_installations").fetchone()["n"] == 1
        assert b.execute("SELECT team_id FROM slack_installations").fetchone()["team_id"] == "T-BETA"
        assert secrets.get(b, TA, "slack_bot_token") is None  # A's row is invisible, not merely filtered out
        assert secrets.get(b, TB, "slack_bot_token") == TOKEN_B
        # the SECURITY DEFINER lookup still answers (that is how an inbound event finds its tenant) …
        assert slack_install.lookup_team(b, "T-ALPHA")["tenant_id"] == TA
        # … and it carries no credential
        assert TOKEN_A not in json.dumps(slack_install.lookup_team(b, "T-ALPHA"), default=str)


def test_interactivity_and_status_degrade_cleanly_without_the_env(client, monkeypatch):
    raw = interactivity_body({"type": "block_actions", "team": {"id": "T-ALPHA"}, "actions": []})
    headers = signed(raw, content_type="application/x-www-form-urlencoded")
    monkeypatch.delenv("STARTUPOS_SLACK_SIGNING_SECRET", raising=False)
    r = client.post("/slack/interactivity", content=raw, headers=headers)
    assert r.status_code == 503 and "not configured" in r.text

    monkeypatch.delenv("STARTUPOS_SLACK_CLIENT_ID", raising=False)
    status = as_tenant(client, TA).get("/slack/status").json()
    assert status["configured"] is False and status["connected"] is False
    assert client.get("/slack/oauth/callback", params={"code": "c", "state": "s"}).status_code == 503
