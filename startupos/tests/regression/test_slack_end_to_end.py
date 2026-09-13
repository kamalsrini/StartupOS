"""Sprint 3b regression: the WHOLE Slack chain, end to end, for two companies in two workspaces.

The per-track suites each prove their own seam (install, delivery, buttons, the inbound queue). This file is the
one black-box walk of the path a founder actually takes, with nothing between the steps but the product:

    sign up → install Slack over the real OAuth callback → ingest → the signal engine → a daemon tick posts the
    high signal → a skill proposes an approval with Approve/Decline buttons → a signed press on
    /slack/interactivity decides it through the gate, runs the executor with that tenant's credential and edits
    the message in place → a DM on /slack/events is acknowledged inside Slack's budget, drains through
    `tenant_jobs` and is answered → `app_uninstalled` revokes everything and the next tick is a clean skip.

Two tenants do all of it at once, in two workspaces, and every step asserts that the other one saw nothing: its
channel, its bot token, its ledger, its approvals. Every connection below the API is an RLS-bound `startupos_app`
connection, and the API itself runs on that role too (STARTUPOS_API_DSN), because isolation is the claim.

Offline and deterministic: Slack's HTTP surface (oauth.v2.access, chat.postMessage, chat.update), Linear's
GraphQL and the Anthropic client are all injected; nothing here opens a socket and no assertion depends on the
wall clock. What this CANNOT prove is listed at the bottom of the file — that list is the real-Slack checklist.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from track_b_fakes import FakeLinearGraphQL, seed_team_doc

from auth import slack_install, slack_sig
from common import secrets
from common.db import apply_schema
from common.models import Approval, Exec
from daemon import approvals, delivery, llm, scheduler, slack_actions
from daemon import jobs as daemon_jobs
from daemon.executors import linear as linear_executor
from daemon.executors import slack as slack_executor
from ingest import runner
from tests.conftest import APP_TEST_DSN, app_conn_for
from tests.functional.test_slack_install import FakeOAuth, oauth_response

pytestmark = pytest.mark.regression

REPO = Path(__file__).resolve().parents[2]

BOOT_TOKEN = "bootstrap-test-token"  # noqa: S105 - test fixture
SIGNING_SECRET = "e2e-slack-signing-secret"  # noqa: S105 - test fixture

# Two companies. The tenant id is what POST /onboarding/tenant slugifies the name into.
A = {
    "name": "E2E Alpha",
    "tenant": "e2e-alpha",
    "email": "founder@e2e-alpha.test",
    "team": "T-E2E-ALPHA",
    "team_name": "Alpha Workspace",
    "slack_user": "U-E2E-ALPHA",
    "bot_token": "xoxb-e2e-alpha-token",
    "channel": "#alpha-ops",
    "dm": "D-ALPHA",
    "linear_key": "lin_api_e2e_alpha",
}
B = {
    "name": "E2E Beta",
    "tenant": "e2e-beta",
    "email": "founder@e2e-beta.test",
    "team": "T-E2E-BETA",
    "team_name": "Beta Workspace",
    "slack_user": "U-E2E-BETA",
    "bot_token": "xoxb-e2e-beta-token",
    "channel": "#beta-ops",
    "dm": "D-BETA",
    "linear_key": "lin_api_e2e_beta",
}
BOTH = (A, B)

TEAM_MD = "# Team\n- Alexey — workspace, graph, build, api, backend\n- Maria — web, onboarding, frontend\n"
APPROVAL_ID = "assign-acm-158"  # common.ids.approval_id("assign", "ACM-158")
BETA_ONLY_APPROVAL = "beta-only-assign"  # an id tenant A has no row for, so a cross-tenant press has a real target
HIGH_SIGNAL = "build.unassigned_high:ACM-158"  # the one fixture signal no rule dates out

# Every tenant table the two companies touch, child-first so the deletes never trip an FK.
TABLES = (
    "deliveries",
    "tenant_jobs",
    "approvals",
    "runs",
    "signals",
    "events",
    "issues",
    "projects",
    "messages",
    "transactions",
    "cards",
    "bills",
    "vendors",
    "accounts_bank",
    "deployments",
    "budgets",
    "context_packs",
    "brain_docs",
    "sessions",
    "api_tokens",
    "connections",
    "tenant_secrets",
    "users",
)


# --- the fake Slack HTTP surface ------------------------------------------------------------------


class FakeSlack:
    """Both Slack write calls, for both workspaces, recorded with the token each was made with.

    `daemon/executors/slack.py` is the only Slack write path in the product, so replacing its two functions
    replaces the whole outbound surface — delivery, the approval announcement, the approved executor, the
    daemon's DM reply and `chat.update` all land here.
    """

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    def post(self, inp: dict[str, Any], *, client: Any = None, token: str | None = None) -> dict[str, Any]:
        if not token:  # what the real client does with no credential — never silently post as somebody else
            raise slack_executor.SlackError("no Slack credential for this tenant")
        ts = f"17255000{len(self.posts) + 1:04d}.0001"
        self.posts.append(
            {
                "channel": inp["channel"],
                "text": inp.get("text") or "",
                "blocks": inp.get("blocks"),
                "thread_ts": inp.get("thread_ts"),
                "token": token,
                "ts": ts,
            }
        )
        return {"text": "Posted", "url": "https://slack.test/archives/x", "ts": ts, "channel": inp["channel"]}

    def update(self, inp: dict[str, Any], *, client: Any = None, token: str | None = None) -> dict[str, Any]:
        if not token:
            raise slack_executor.SlackError("no Slack credential for this tenant")
        self.updates.append(
            {
                "channel": inp["channel"],
                "ts": inp["ts"],
                "text": inp.get("text") or "",
                "blocks": inp.get("blocks"),
                "token": token,
            }
        )
        return {"text": "Updated", "ts": inp["ts"], "channel": inp["channel"]}

    # --- what a workspace saw -------------------------------------------------------------------
    def seen_by(self, company: dict[str, str]) -> list[dict[str, Any]]:
        """Everything that reached this company's workspace — by its bot token, which is the real boundary."""
        return [c for c in self.posts + self.updates if c["token"] == company["bot_token"]]

    def posts_to(self, channel: str) -> list[dict[str, Any]]:
        return [p for p in self.posts if p["channel"] == channel]

    def tokens(self) -> set[str]:
        return {c["token"] for c in self.posts + self.updates}


class RecordingLinear:
    """Linear's GraphQL transport, recording the api_key the executor resolved for each call."""

    def __init__(self) -> None:
        self.inner = FakeLinearGraphQL()
        self.keys: list[str | None] = []

    def __call__(self, query: str, variables: dict[str, Any], api_key: str | None = None) -> dict[str, Any]:
        self.keys.append(api_key)
        return self.inner(query, variables)


# --- helpers --------------------------------------------------------------------------------------


def signed(body: bytes, *, secret: str = SIGNING_SECRET, content_type: str = "application/json") -> dict[str, str]:
    """Headers Slack would send: the replay window is real, so sign with a real clock."""
    ts = int(time.time())
    return {
        "X-Slack-Request-Timestamp": str(ts),
        "X-Slack-Signature": slack_sig.sign(ts, body, secret),
        "Content-Type": content_type,
    }


def post_event(client: TestClient, body: dict[str, Any]) -> Any:
    raw = json.dumps(body).encode()
    return client.post("/slack/events", content=raw, headers=signed(raw))


def post_press(client: TestClient, payload: dict[str, Any]) -> Any:
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    return client.post(
        "/slack/interactivity", content=raw, headers=signed(raw, content_type="application/x-www-form-urlencoded")
    )


def press(action: str, *, team: str, user: str, value: str) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "actions": [{"action_id": action, "value": value, "type": "button"}],
        "response_url": "https://hooks.slack.test/actions/1",
    }


def ledger(tenant_id: str) -> list[tuple[str, str, str | None, str]]:
    with app_conn_for(tenant_id) as c:
        return [
            (r["kind"], r["ref"], r["channel"], r["status"])
            for r in c.execute(
                "SELECT kind, ref, channel, status FROM deliveries WHERE tenant_id = %s ORDER BY id", (tenant_id,)
            ).fetchall()
        ]


def approval_row(tenant_id: str, approval_id: str) -> dict[str, Any] | None:
    with app_conn_for(tenant_id) as c:
        row = c.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,)).fetchone()
    return dict(row) if row else None


def open_high_signals(tenant_id: str) -> list[str]:
    with app_conn_for(tenant_id) as c:
        return [
            r["id"]
            for r in c.execute(
                """SELECT id FROM signals WHERE tenant_id = %s AND resolved_at IS NULL
                     AND (severity = 'high' OR rule_id LIKE 'cos.risk%%') ORDER BY first_seen_at, id""",
                (tenant_id,),
            ).fetchall()
        ]


def button_action_ids(blocks: list[dict[str, Any]] | None) -> list[str]:
    return [
        el.get("action_id")
        for b in (blocks or [])
        for el in (b.get("elements") or [])
        if isinstance(el, dict) and el.get("type") == "button"
    ]


def button_values(blocks: list[dict[str, Any]] | None) -> list[str]:
    return [
        str(el.get("value"))
        for b in (blocks or [])
        for el in (b.get("elements") or [])
        if isinstance(el, dict) and el.get("type") == "button"
    ]


def wipe(conn) -> None:
    for company in BOTH:
        for table in TABLES:
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (company["tenant"],))
        conn.execute(
            "DELETE FROM slack_installations WHERE tenant_id = %s OR team_id = %s",
            (company["tenant"], company["team"]),
        )
        conn.execute("DELETE FROM tenants WHERE id = %s", (company["tenant"],))
    conn.commit()


@contextmanager
def _tenant_conn(*_a: Any, tenant_id: str | None = None, **_k: Any):
    """What the daemon's `get_conn` does in production: a connection bound to one tenant, under RLS."""
    with app_conn_for(tenant_id) as c:
        yield c


# --- fixtures -------------------------------------------------------------------------------------


@pytest.fixture()
def chain(conn, monkeypatch):
    """One install-level Slack app, one master key, two empty companies, and no model configured."""
    monkeypatch.setenv("STARTUPOS_API_DSN", APP_TEST_DSN)  # the API runs on the RLS role, as it does in production
    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", "e2e-session-secret-not-for-production")
    monkeypatch.setenv("STARTUPOS_BOOTSTRAP_TOKEN", BOOT_TOKEN)
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("STARTUPOS_SLACK_CLIENT_ID", "1111.2222")
    monkeypatch.setenv("STARTUPOS_SLACK_CLIENT_SECRET", "e2e-slack-client-secret")
    monkeypatch.setenv("STARTUPOS_SLACK_SIGNING_SECRET", SIGNING_SECRET)
    monkeypatch.setenv("STARTUPOS_WEB_URL", "https://app.startupos.test")
    monkeypatch.setenv("STARTUPOS_PUBLIC_URL", "https://api.startupos.test")
    monkeypatch.delenv("STARTUPOS_DEV", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # every skill falls back to Tier 0 — no model, no network
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)  # the operator token must never stand in for a tenant's
    monkeypatch.setattr(llm, "_client_factory", llm._default_client_factory)

    slack = FakeSlack()
    monkeypatch.setattr(slack_executor, "post_message", slack.post)
    monkeypatch.setattr(slack_executor, "update_message", slack.update)
    linear = RecordingLinear()
    # `_transport` hands the tenant's own key to _default_graphql only when _graphql has not been replaced;
    # replacing both keeps that branch live, so the key the executor resolved is visible to the test.
    monkeypatch.setattr(linear_executor, "_default_graphql", linear)
    monkeypatch.setattr(linear_executor, "_graphql", linear)
    monkeypatch.setattr(scheduler, "get_conn", _tenant_conn)
    monkeypatch.setattr(daemon_jobs, "BACKOFF_SECONDS", (0, 0))

    wipe(conn)
    from api.main import app

    with TestClient(app, follow_redirects=False) as client:
        yield {"client": client, "slack": slack, "linear": linear, "conn": conn}
    wipe(conn)


def sign_up(client: TestClient, company: dict[str, str]) -> str:
    """POST /onboarding/tenant — the only unauthenticated write. Returns the session cookie."""
    r = client.post(
        "/onboarding/tenant",
        json={
            "name": company["name"],
            "website": f"https://{company['tenant']}.test",
            "email": company["email"],
            "bootstrap_token": BOOT_TOKEN,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["tenant"]["id"] == company["tenant"]
    cookie = client.cookies.get("sos_session")
    assert cookie
    client.cookies.clear()
    return cookie


def as_company(client: TestClient, cookie: str) -> TestClient:
    client.cookies.clear()
    client.cookies.set("sos_session", cookie)
    return client


def install_slack(client: TestClient, monkeypatch, company: dict[str, str], cookie: str) -> Any:
    """The real OAuth round trip: signed state out, a faked `oauth.v2.access` back, over the real callback route."""
    body = oauth_response(company["team"], company["team_name"], company["bot_token"], bot_user_id="U0BOT")
    body["incoming_webhook"] = {"channel": company["channel"]}  # the channel the founder picked while installing
    monkeypatch.setattr(slack_install, "_http", FakeOAuth(body))

    r = as_company(client, cookie).get("/slack/install")
    assert r.status_code == 302, r.text
    state = r.headers["location"].split("state=")[1].split("&")[0]
    client.cookies.clear()  # Slack sends the browser back with no session — the signed state is the only identity
    return client.get("/slack/oauth/callback", params={"code": f"code-{company['tenant']}", "state": state})


# --- the walk -------------------------------------------------------------------------------------


def test_the_whole_slack_chain_for_two_tenants_in_two_workspaces(chain, monkeypatch):
    client, slack, linear, conn = chain["client"], chain["slack"], chain["linear"], chain["conn"]
    cookies: dict[str, str] = {}

    # === 1. sign up and install Slack ============================================================
    for company in BOTH:
        cookies[company["tenant"]] = sign_up(client, company)
        r = install_slack(client, monkeypatch, company, cookies[company["tenant"]])
        assert r.status_code == 302
        assert r.headers["location"] == "https://app.startupos.test/settings?slack=connected"

    for company in BOTH:
        tid = company["tenant"]
        with app_conn_for(tid) as c:
            install = c.execute("SELECT * FROM slack_installations WHERE tenant_id = %s", (tid,)).fetchone()
            assert install["team_id"] == company["team"] and install["revoked_at"] is None
            assert install["default_channel"] == company["channel"]
            assert company["bot_token"] not in json.dumps(dict(install), default=str)  # no token in this table

            # the bot token exists once, encrypted, and only as this tenant's secret
            assert secrets.get(c, tid, "slack_bot_token") == company["bot_token"]
            ciphertext = c.execute(
                "SELECT ciphertext FROM tenant_secrets WHERE tenant_id = %s AND name = 'slack_bot_token'", (tid,)
            ).fetchone()["ciphertext"]
            assert company["bot_token"].encode() not in bytes(ciphertext)

            cx = c.execute("SELECT * FROM connections WHERE tenant_id = %s AND source = 'slack'", (tid,)).fetchone()
            assert cx["secret_ref"] == "kv:slack_bot_token" and cx["status"] == "connected"
            assert cx["config"]["team_id"] == company["team"]
            # and this is what every later step resolves
            assert secrets.credential_for_source(c, tid, "slack") == company["bot_token"]

        status = as_company(client, cookies[tid]).get("/slack/status").json()
        assert status == {**status, "connected": True, "configured": True, "team_id": company["team"]}
        assert company["bot_token"] not in json.dumps(status)

    # one workspace, one company — in both directions, through the SECURITY DEFINER lookup
    with app_conn_for(A["tenant"]) as c:
        assert slack_install.lookup_team(c, B["team"])["tenant_id"] == B["tenant"]
        assert c.execute("SELECT count(*) AS n FROM slack_installations").fetchone()["n"] == 1  # RLS: only its own
        assert secrets.get(c, B["tenant"], "slack_bot_token") is None

    # The founder's channel and the rest of the per-tenant setup — every step a route the web app calls.
    for company in BOTH:
        tid = company["tenant"]
        r = as_company(client, cookies[tid]).post(
            "/onboarding/cadence", json={"channel": "slack", "pulse_hour": 7, "slack_channel": company["channel"]}
        )
        assert r.status_code == 200 and r.json()["channel"] == "slack"
        assert r.json()["slack_channel"] == company["channel"]
        # …and Settings shows that channel as the one in effect, over the channel the install captured
        status = as_company(client, cookies[tid]).get("/slack/status").json()
        assert status["channel"] == company["channel"] and status["delivers"] is True
        r = as_company(client, cookies[tid]).post(
            "/onboarding/connections", json={"source": "linear", "credential": company["linear_key"]}
        )
        assert r.status_code == 200 and company["linear_key"] not in r.text
        conn.execute("UPDATE users SET slack_user_id = %s WHERE tenant_id = %s", (company["slack_user"], tid))
    conn.commit()
    assert slack.posts == [] and slack.updates == []  # installing posts nothing

    # === 2. ingest → the signal engine → a daemon tick delivers the high signal ===================
    for company in BOTH:
        with app_conn_for(company["tenant"]) as c:
            out = runner.run_all(c, company["tenant"], force_fixtures=True)
            assert set(out) == {"linear", "slack", "brex", "vercel"} and all(t is not None for t in out.values())
            seed_team_doc(c, TEAM_MD, tenant=company["tenant"])
            # the Linear connection keeps the key the founder handed the API, not the ingest bootstrap ref
            assert secrets.credential_for_source(c, company["tenant"], "linear") == company["linear_key"]

    scheduler.tick_15m(A["tenant"])  # signal engine → signal skills → delivery → executors, exactly as in prod

    highs = open_high_signals(A["tenant"])
    assert HIGH_SIGNAL in highs, highs
    a_ledger = dict((ref, status) for _kind, ref, _ch, status in ledger(A["tenant"]))
    assert a_ledger[f"signal:{HIGH_SIGNAL}"] == "sent"
    delivered = [ref for ref in a_ledger if ref.startswith("signal:")]
    assert delivered == [f"signal:{s}" for s in highs[: delivery.SIGNALS_PER_TICK]]  # 5 per tick, oldest first

    # everything went to A's channel with A's token; B's workspace saw nothing at all
    assert {p["channel"] for p in slack.posts} == {A["channel"]}
    assert slack.tokens() == {A["bot_token"]}
    assert slack.seen_by(B) == []
    assert ledger(B["tenant"]) == []
    signal_post = next(p for p in slack.posts if HIGH_SIGNAL.split(":")[1] in p["text"] or "ACM-158" in p["text"])
    assert signal_post["token"] == A["bot_token"] and signal_post["text"].strip()  # a notification fallback, always

    # a second tick posts nothing twice: the deliveries UNIQUE constraint is the dedupe, not a query
    refs_before = [ref for _k, ref, _c, _s in ledger(A["tenant"]) if ref.startswith("signal:")]
    posts_after_first_tick = len(slack.posts)
    scheduler.tick_15m(A["tenant"])
    refs_after = [ref for _k, ref, _c, _s in ledger(A["tenant"]) if ref.startswith("signal:")]
    assert len(refs_after) == len(set(refs_after))  # one ledger row per signal, ever
    assert refs_after[: len(refs_before)] == refs_before  # nothing already delivered was delivered again
    assert set(refs_after) == {f"signal:{s}" for s in open_high_signals(A["tenant"])}  # the backlog drained
    assert len(slack.posts) - posts_after_first_tick == len(refs_after) - len(refs_before)

    # === 3. a skill proposed an approval, and it carries the buttons ==============================
    proposal = approval_row(A["tenant"], APPROVAL_ID)
    assert proposal is not None and proposal["status"] == "pending"
    assert proposal["exec"]["server"] == "Linear" and proposal["exec"]["input"]["id"] == "ACM-158"
    assert proposal["signal_id"] == HIGH_SIGNAL  # the skill ran off the signal the tick had just raised

    approval_post = next(p for p in slack.posts if APPROVAL_ID in json.dumps(p["blocks"] or []))
    assert approval_post["channel"] == A["channel"] and approval_post["token"] == A["bot_token"]
    assert button_action_ids(approval_post["blocks"]) == ["approval_approve", "approval_decline"]
    assert button_values(approval_post["blocks"]) == [APPROVAL_ID, APPROVAL_ID]  # both buttons carry the id
    posted = next(row for row in ledger(A["tenant"]) if row[1] == f"approval:{APPROVAL_ID}")
    assert posted[3] == "sent" and posted[2] == A["channel"]
    assert slack.seen_by(B) == []

    # === 4. a real signed press decides it through the gate ======================================
    # 4a. B runs its own tick (same fixtures, so the same approval ids — the PK is (tenant_id, id)), plus one
    #     approval only B has, which is what a cross-tenant press can actually aim at.
    scheduler.tick_15m(B["tenant"])
    posts_before_beta = len(slack.posts)
    with app_conn_for(B["tenant"]) as c:
        approvals.propose(
            c,
            Approval(
                id=BETA_ONLY_APPROVAL,
                tenant_id=B["tenant"],
                module="build",
                type="Linear · assign",
                target="ACM-900 → Maria",
                preview="Assign ACM-900 to Maria",
                exec=Exec(server="Linear", tool="save_issue", input={"id": "ACM-900", "assignee": "Maria"}),
            ),
        )
    beta_post = next(p for p in slack.posts[posts_before_beta:] if BETA_ONLY_APPROVAL in json.dumps(p["blocks"] or []))
    assert beta_post["channel"] == B["channel"] and beta_post["token"] == B["bot_token"]
    assert approval_row(A["tenant"], BETA_ONLY_APPROVAL) is None  # A has no such row at all

    before = json.dumps(approval_row(B["tenant"], BETA_ONLY_APPROVAL), default=str)
    executed_before = len(linear.keys)
    updates_before_cross = len(slack.updates)

    # tenant A's founder, in tenant A's workspace, presses a button carrying tenant B's approval id.
    r = post_press(client, press("approval_approve", team=A["team"], user=A["slack_user"], value=BETA_ONLY_APPROVAL))
    assert r.status_code == 200 and r.json()["text"] == slack_actions.GONE  # invisible under RLS, not "forbidden"
    assert json.dumps(approval_row(B["tenant"], BETA_ONLY_APPROVAL), default=str) == before
    assert len(linear.keys) == executed_before  # no executor ran
    assert len(slack.updates) == updates_before_cross  # and B's message was not touched

    # 4b. B's founder pressing A's button is exactly as unauthorised (a stranger to that workspace).
    r = post_press(client, press("approval_approve", team=A["team"], user=B["slack_user"], value=APPROVAL_ID))
    assert r.status_code == 200 and "Link your StartupOS account" in r.json()["text"]
    assert approval_row(A["tenant"], APPROVAL_ID)["status"] == "pending"

    # 4c. the real press.
    updates_before = len(slack.updates)
    r = post_press(client, press("approval_approve", team=A["team"], user=A["slack_user"], value=APPROVAL_ID))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "executed" and body["decided"] is True

    decided = approval_row(A["tenant"], APPROVAL_ID)
    assert decided["status"] == "executed" and decided["decided_by"]
    assert decided["result"]["url"].endswith("/ACM-158")
    assert linear.keys[executed_before:] and set(linear.keys[executed_before:]) == {A["linear_key"]}  # A's own key
    issue_update = next(v for q, v in linear.inner.mutations if "issueUpdate" in q)
    assert issue_update["input"]["assigneeId"] == "user-uuid-alexey"

    # the message was edited in place, in A's channel, at the ts the ledger recorded, with A's token
    edit = slack.updates[updates_before]
    assert edit["channel"] == A["channel"] and edit["ts"] == approval_post["ts"] and edit["token"] == A["bot_token"]
    assert button_action_ids(edit["blocks"]) == []  # the buttons are gone, so they cannot be pressed twice

    # 4d. pressing again is a no-op: it decides nothing, executes nothing, and just re-renders.
    keys_after = len(linear.keys)
    again = post_press(client, press("approval_approve", team=A["team"], user=A["slack_user"], value=APPROVAL_ID))
    assert again.status_code == 200 and again.json()["decided"] is False and again.json()["status"] == "executed"
    assert len(linear.keys) == keys_after
    assert approval_row(A["tenant"], APPROVAL_ID)["decided_by"] == decided["decided_by"]
    assert len(slack.updates) == updates_before + 2  # re-rendered, never re-executed
    assert {c["token"] for c in slack.posts + slack.updates if c["channel"] == A["channel"]} == {A["bot_token"]}
    assert approval_row(B["tenant"], APPROVAL_ID)["status"] == "pending"  # B's same-id approval is untouched

    # === 5. a DM arrives, is acknowledged inside Slack's budget, and is answered ==================
    started = time.perf_counter()
    r = post_event(
        client,
        {
            "type": "event_callback",
            "team_id": A["team"],
            "event_id": "Ev-E2E-DM",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": A["slack_user"],
                "channel": A["dm"],
                "text": "pending",
            },
        },
    )
    ack_seconds = time.perf_counter() - started
    assert r.status_code == 200 and r.json() == {"ok": True, "queued": True}
    assert ack_seconds < 3.0, f"the ack took {ack_seconds:.2f}s — Slack's budget is 3s"

    with app_conn_for(A["tenant"]) as c:
        job = c.execute("SELECT * FROM tenant_jobs WHERE kind = 'slack_event'").fetchone()
        assert job["tenant_id"] == A["tenant"] and job["status"] == "queued"
    with app_conn_for(B["tenant"]) as c:
        assert not c.execute("SELECT 1 FROM tenant_jobs WHERE kind = 'slack_event'").fetchone()  # never B's queue

    posts_before = len(slack.posts)
    out = daemon_jobs.drain(tenants=[A["tenant"]], dsn=APP_TEST_DSN)
    assert out["done"] >= 1 and out["failed"] == 0, out

    replies = slack.posts[posts_before:]
    assert len(replies) == 1
    reply = replies[0]
    assert reply["channel"] == A["dm"] and reply["token"] == A["bot_token"]
    assert APPROVAL_ID not in reply["text"]  # it was executed in step 4, so it is no longer pending
    assert reply["text"].strip()
    with app_conn_for(A["tenant"]) as c:
        assert c.execute("SELECT status FROM tenant_jobs WHERE kind='slack_event'").fetchone()["status"] == "done"

    # a Slack retry of the same event_id answers once
    assert post_event(
        client,
        {
            "type": "event_callback",
            "team_id": A["team"],
            "event_id": "Ev-E2E-DM",
            "event": {
                "type": "message",
                "channel_type": "im",
                "user": A["slack_user"],
                "channel": A["dm"],
                "text": "pending",
            },
        },
    ).json() == {"ok": True, "queued": False}
    assert daemon_jobs.drain(tenants=[A["tenant"]], dsn=APP_TEST_DSN)["claimed"] == 0
    assert len(slack.posts) == posts_before + 1

    # === 6. uninstall: revoke everything, and the next tick is a clean skip =======================
    r = post_event(
        client,
        {
            "type": "event_callback",
            "team_id": A["team"],
            "event_id": "Ev-E2E-BYE",
            "event": {"type": "app_uninstalled"},
        },
    )
    assert r.status_code == 200 and r.json()["handled"] == "app_uninstalled"

    with app_conn_for(A["tenant"]) as c:
        assert c.execute("SELECT revoked_at FROM slack_installations").fetchone()["revoked_at"] is not None
        assert secrets.get(c, A["tenant"], "slack_bot_token") is None
        assert c.execute("SELECT status FROM connections WHERE source='slack'").fetchone()["status"] == "disabled"
        assert secrets.credential_for_source(c, A["tenant"], "slack") is None
        # something new and undelivered, so the next tick has real work to skip
        c.execute(
            """INSERT INTO signals (id, tenant_id, module, rule_id, severity, kind, title)
               VALUES ('cos.risk:after-uninstall', %s, 'cos', 'cos.risk', 'high', 'reply', 'Runway dips')""",
            (A["tenant"],),
        )

    posts_before = len(slack.posts)
    results = scheduler.deliver_signals(A["tenant"])  # must not raise, must not post, must not blacklist the ref
    assert [d["status"] for d in results] == ["skipped"] * len(results) and results
    assert all("credential" in d["reason"] for d in results)
    assert len(slack.posts) == posts_before
    assert "signal:cos.risk:after-uninstall" not in [ref for _k, ref, _c, _s in ledger(A["tenant"])]

    # an inbound event from the revoked workspace is a clean 200 that queues nothing
    r = post_event(
        client,
        {
            "type": "event_callback",
            "team_id": A["team"],
            "event_id": "Ev-E2E-AFTER",
            "event": {"type": "message", "channel_type": "im", "user": A["slack_user"], "channel": A["dm"], "t": "x"},
        },
    )
    assert r.status_code == 200 and "not connected" in r.json()["text"]
    with app_conn_for(A["tenant"]) as c:
        assert not c.execute("SELECT 1 FROM tenant_jobs WHERE kind='slack_event' AND status <> 'done'").fetchone()
    pressed = post_press(client, press("approval_approve", team=A["team"], user=A["slack_user"], value=APPROVAL_ID))
    assert pressed.status_code == 200 and "not connected" in pressed.json()["text"]

    # and B is untouched by any of it: still installed, still delivering, with its own token
    assert as_company(client, cookies[B["tenant"]]).get("/slack/status").json()["connected"] is True
    with app_conn_for(B["tenant"]) as c:
        c.execute(
            """INSERT INTO signals (id, tenant_id, module, rule_id, severity, kind, title)
               VALUES ('cos.risk:beta-still-works', %s, 'cos', 'cos.risk', 'high', 'reply', 'Beta risk')""",
            (B["tenant"],),
        )
    posts_before = len(slack.posts)
    assert [d["status"] for d in scheduler.deliver_signals(B["tenant"])] == ["sent"]
    new_posts = slack.posts[posts_before:]
    assert len(new_posts) == 1 and new_posts[0]["channel"] == B["channel"]
    assert new_posts[0]["token"] == B["bot_token"]

    # the whole run: exactly two tokens ever used, each only in its own channel
    assert slack.tokens() == {A["bot_token"], B["bot_token"]}
    for company, other in ((A, B), (B, A)):
        assert all(c["channel"] != other["channel"] for c in slack.seen_by(company))


# --- the two operational claims the deploy rests on -----------------------------------------------


SLACK_ENV = ("STARTUPOS_SLACK_CLIENT_ID", "STARTUPOS_SLACK_CLIENT_SECRET", "STARTUPOS_SLACK_SIGNING_SECRET")


def test_the_app_imports_and_serves_without_the_slack_env(conn, monkeypatch):
    """Claim one: a deploy that has not created the Slack app yet still boots; only /slack/* degrades.

    The import is checked in a fresh interpreter, because `api.main` is already imported in this one and an
    import-time read of a missing env var would not show up here otherwise.
    """
    env = {k: v for k, v in os.environ.items() if k not in SLACK_ENV}
    env["PYTHONPATH"] = str(REPO)
    proc = subprocess.run(  # noqa: S603 - our own interpreter, fixed argv
        [sys.executable, "-c", "import api.main, daemon.main, daemon.delivery, daemon.slack_actions; print('ok')"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("ok")

    from tests.conftest import login_as

    monkeypatch.setenv("STARTUPOS_API_DSN", APP_TEST_DSN)
    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", "e2e-session-secret-not-for-production")
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    for key in SLACK_ENV:
        monkeypatch.delenv(key, raising=False)
    _user, cookie = login_as(conn, "unitone", "noslack@unitone.ai")

    from api.main import app

    with TestClient(app, follow_redirects=False) as client:
        assert client.get("/health").status_code == 200  # the app is up, the database is up
        assert client.get("/health").json()["ok"] is True
        assert client.get("/").status_code == 200

        raw = json.dumps({"type": "url_verification", "challenge": "c"}).encode()
        assert client.post("/slack/events", content=raw, headers=signed(raw)).status_code == 503
        form = urlencode({"payload": json.dumps({"type": "block_actions", "team": {"id": "T"}})}).encode()
        r = client.post(
            "/slack/interactivity", content=form, headers=signed(form, content_type="application/x-www-form-urlencoded")
        )
        assert r.status_code == 503 and "not configured" in r.text
        assert client.get("/slack/oauth/callback", params={"code": "c", "state": "s"}).status_code == 503

        client.cookies.set("sos_session", cookie)
        r = client.get("/slack/install")
        assert r.status_code == 503 and "STARTUPOS_SLACK_CLIENT_ID" in r.text
        # /slack/status is the one Slack route that still answers: Settings has to render "not configured"
        status = client.get("/slack/status").json()
        assert status["configured"] is False and status["connected"] is False


FORCED_RLS_TABLES = ("tenants", "signals", "approvals", "deliveries", "slack_installations", "tenant_secrets")


def test_schema_and_rls_apply_twice_over_a_populated_database(test_dsn, conn):
    """Claim two: `make db` is safe to re-run on a live database — the deploy applies schema.sql + rls.sql again.

    Same property as the Sprint 3a forced-RLS check in tests/functional/test_rls.py, but across a re-apply: the
    rows, the policies, the SECURITY DEFINER lookups and the `startupos_app` grants all have to survive it.
    """
    tenant = "e2e-reapply"
    conn.execute("DELETE FROM signals WHERE tenant_id = %s", (tenant,))
    conn.execute("DELETE FROM slack_installations WHERE tenant_id = %s", (tenant,))
    conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
    conn.execute(
        "INSERT INTO tenants (id, name, pulse_channel, slack_channel) VALUES (%s, %s, 'slack', '#ops')",
        (tenant, "Reapply Co"),
    )
    conn.execute(
        """INSERT INTO slack_installations (tenant_id, team_id, team_name, bot_user_id, default_channel)
           VALUES (%s, 'T-REAPPLY', 'Reapply', 'U0BOT', '#ops')""",
        (tenant,),
    )
    conn.execute(
        """INSERT INTO signals (id, tenant_id, module, rule_id, severity, kind, title)
           VALUES ('reapply:1', %s, 'build', 'build.unassigned_high', 'high', 'click', 'keep me')""",
        (tenant,),
    )
    conn.commit()

    def snapshot() -> dict[str, Any]:
        rows = {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in FORCED_RLS_TABLES}
        cols = {
            (r["table_name"], r["column_name"])
            for r in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
            ).fetchall()
        }
        conn.commit()  # release every ACCESS SHARE lock: schema.sql's ALTER TABLEs would queue behind them
        return {"rows": rows, "cols": cols}

    before = snapshot()
    for _ in range(2):  # twice more, on top of the apply the fixture already did — three in total
        apply_schema(test_dsn)
    after = snapshot()

    assert after["rows"] == before["rows"], "re-applying the schema lost or duplicated rows"
    assert after["cols"] == before["cols"], "re-applying the schema changed the shape of a table"
    kept = conn.execute("SELECT title FROM signals WHERE tenant_id = %s AND id = 'reapply:1'", (tenant,)).fetchone()
    assert kept["title"] == "keep me"
    install = conn.execute("SELECT team_id FROM slack_installations WHERE tenant_id = %s", (tenant,)).fetchone()
    assert install["team_id"] == "T-REAPPLY"

    # RLS is still forced on every tenant table, and the app role still has what the daemon needs
    forced = {
        r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"])
        for r in conn.execute(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)",
            (list(FORCED_RLS_TABLES),),
        ).fetchall()
    }
    assert all(state == (True, True) for state in forced.values()), forced
    with app_conn_for(tenant) as c:
        assert c.execute("SELECT count(*) AS n FROM signals").fetchone()["n"] == 1  # only its own, still
        assert slack_install.lookup_team(c, "T-REAPPLY")["tenant_id"] == tenant  # SECURITY DEFINER survived
        assert c.execute("SELECT count(*) AS n FROM tenants_active()").fetchone()["n"] >= 1

    conn.execute("DELETE FROM signals WHERE tenant_id = %s", (tenant,))
    conn.execute("DELETE FROM slack_installations WHERE tenant_id = %s", (tenant,))
    conn.execute("DELETE FROM tenants WHERE id = %s", (tenant,))
    conn.commit()


# --- what this file cannot prove (the real-Slack checklist) ---------------------------------------
#
# Everything above runs offline, so the following are assumptions about Slack itself, not facts:
#   * the OAuth app's redirect URL, scopes and distribution settings match `auth/slack_install.SCOPES`;
#   * `oauth.v2.access` really returns the body shape in tests/functional/test_slack_install.oauth_response;
#   * Slack accepts the Block Kit this build renders (validate() checks our own rules, not Slack's parser);
#   * the bot is actually in the channel `tenants.slack_channel` names (chat:write.public covers public channels
#     only — a private channel needs an invite, and the real failure is `not_in_channel`);
#   * Slack's 3-second budget is met on real network latency, not just in-process;
#   * `chat.update` accepts a `ts` this old, and Slack's own retry/rate-limit behaviour (`ratelimited`, 429);
#   * the Events API subscriptions (app_mention, message.im, app_uninstalled, tokens_revoked) are enabled and the
#     request URL is verified;
#   * interactivity is enabled with the same request URL and the signing secret matches.
