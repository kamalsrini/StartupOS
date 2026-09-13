"""Track B end-to-end: a founder presses Approve in Slack and the work happens (CONTRACTS.md Sprint 3b, Track B).

The whole path, nothing stubbed but the network: Slack signs a button press → `POST /slack/interactivity` verifies
the signature → the workspace resolves to its installation → the presser resolves to a user of that tenant → the
approval gate decides → the executor acts with THAT tenant's own bot token → the original message is replaced in
place. Every connection below the router is an RLS-bound `startupos_app` connection, because tenant isolation is
the point: tenant B's founder pressing tenant A's button must change nothing at all.

Concurrency is tested for real (two threads, two connections, one approval) — the promise "a double click executes
once" is a promise about `SELECT … FOR UPDATE`, not about a mock.
"""

from __future__ import annotations

import base64
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from auth import slack_sig
from common import secrets
from common.models import Approval, Exec
from daemon import approvals, slack_actions
from daemon.executors import slack as slack_executor
from tests.conftest import app_conn_for

pytestmark = pytest.mark.functional

TA, TB = "btn-alpha", "btn-beta"
TEAM_A, TEAM_B = "T-BTN-ALPHA", "T-BTN-BETA"
USER_A, USER_B = "U-BTN-ALPHA", "U-BTN-BETA"
TOKEN_A, TOKEN_B = "xoxb-btn-alpha", "xoxb-btn-beta"  # noqa: S105 - test fixtures
SIGNING_SECRET = "test-slack-signing-secret"  # noqa: S105 - test fixture
SLACK_EXEC = Exec(server="Slack", tool="post_message", input={"channel": "#build", "text": "Assigned UNI-158"})


class SlackWrites:
    """Both Slack write paths for both tenants, recorded with the token each call was made with."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.slow = False

    def post(self, inp, *, client=None, token=None):
        if not token:
            raise slack_executor.SlackError("no Slack credential for this tenant")
        self.posts.append({"channel": inp["channel"], "text": inp["text"], "blocks": inp.get("blocks"), "token": token})
        if self.slow and inp["channel"] == "#build":
            time.sleep(0.2)  # widen the window a double click has to race in
        return {"text": "Posted", "url": "u", "ts": f"17254000{len(self.posts):02d}.0001", "channel": inp["channel"]}

    def update(self, inp, *, client=None, token=None):
        self.updates.append({"channel": inp["channel"], "ts": inp["ts"], "text": inp["text"], "token": token})
        return {"text": "Updated", "ts": inp["ts"], "channel": inp["channel"]}

    def executed(self) -> list[dict]:
        """Only the posts an EXECUTOR made (the approval messages go to each tenant's own #ops)."""
        return [p for p in self.posts if p["channel"] == "#build"]


@pytest.fixture()
def slack(monkeypatch):
    writes = SlackWrites()
    monkeypatch.setattr(slack_executor, "post_message", writes.post)
    monkeypatch.setattr(slack_executor, "update_message", writes.update)
    return writes


TABLES = ("deliveries", "approvals", "runs", "connections", "tenant_secrets", "users", "brain_docs")


def wipe(conn) -> None:
    """Leave the database exactly as we found it — other suites count installations across all tenants."""
    for tid, team in ((TA, TEAM_A), (TB, TEAM_B)):
        for table in TABLES:
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tid,))
        conn.execute("DELETE FROM slack_installations WHERE tenant_id = %s OR team_id = %s", (tid, team))
        conn.execute("DELETE FROM tenants WHERE id = %s", (tid,))
    conn.commit()


@pytest.fixture()
def workspaces(conn, monkeypatch, auth_env):
    """Two companies, two Slack workspaces, two bot tokens, one linked founder each."""
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("STARTUPOS_SLACK_SIGNING_SECRET", SIGNING_SECRET)
    wipe(conn)
    for tid, team, slack_user, token in ((TA, TEAM_A, USER_A, TOKEN_A), (TB, TEAM_B, USER_B, TOKEN_B)):
        conn.execute(
            """INSERT INTO tenants (id, name, pulse_channel, slack_channel) VALUES (%s, %s, 'slack', '#ops')
               ON CONFLICT (id) DO UPDATE SET pulse_channel = 'slack', slack_channel = '#ops'""",
            (tid, tid),
        )
        conn.execute(
            "INSERT INTO users (id, tenant_id, email, name, slack_user_id) VALUES (%s, %s, %s, 'Founder', %s)",
            (f"{tid}-founder", tid, f"founder@{tid}.test", slack_user),
        )
        secrets.put(conn, tid, "slack_bot_token", token)
        conn.execute(
            """INSERT INTO connections (id, tenant_id, source, secret_ref)
               VALUES (%s, %s, 'slack', 'kv:slack_bot_token')
               ON CONFLICT (tenant_id, source) DO UPDATE SET secret_ref = EXCLUDED.secret_ref""",
            (f"{tid}-slack", tid),
        )
        conn.execute(
            """INSERT INTO slack_installations (tenant_id, team_id, team_name, bot_user_id, default_channel)
               VALUES (%s, %s, %s, 'U0BOT', '#ops')""",
            (tid, team, tid),
        )
    conn.commit()
    yield {TA: f"{TA}-founder", TB: f"{TB}-founder"}
    wipe(conn)


def conn_factory(tenant_id: str):
    """What the router hands the handler, but RLS-bound: a fresh `startupos_app` connection per call."""
    return app_conn_for(tenant_id)


def propose(tenant_id: str, approval_id: str, *, exec_=SLACK_EXEC) -> None:
    with app_conn_for(tenant_id) as c:
        approvals.propose(
            c,
            Approval(
                id=approval_id,
                tenant_id=tenant_id,
                module="build",
                type="issue",
                target="UNI-158",
                preview="Assign UNI-158 to Alexey",
                exec=exec_,
            ),
        )


def approval_row(tenant_id: str, approval_id: str) -> dict[str, Any]:
    with app_conn_for(tenant_id) as c:
        return dict(c.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,)).fetchone())


def delivery_row(tenant_id: str, approval_id: str) -> dict[str, Any]:
    with app_conn_for(tenant_id) as c:
        return dict(c.execute("SELECT * FROM deliveries WHERE ref = %s", (f"approval:{approval_id}",)).fetchone())


def press(action: str, *, team: str, user: str, value: str) -> dict[str, Any]:
    return {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "actions": [{"action_id": action, "value": value, "type": "button"}],
        "response_url": "https://hooks.slack.test/actions/1",
    }


# --- over HTTP, signature and all -----------------------------------------------------------------


@pytest.fixture()
def client(workspaces):
    from api.main import app

    with TestClient(app) as c:
        yield c


def post_press(client: TestClient, payload: dict[str, Any], *, secret: str = SIGNING_SECRET):
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    ts = int(time.time())
    return client.post(
        "/slack/interactivity",
        content=raw,
        headers={
            "X-Slack-Request-Timestamp": str(ts),
            "X-Slack-Signature": slack_sig.sign(ts, raw, secret),
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def test_approve_over_http_executes_and_replaces_the_message(client, slack):
    propose(TA, "ap-http")
    posted = delivery_row(TA, "ap-http")
    assert posted["status"] == "sent" and posted["ts"]

    r = post_press(client, press("approval_approve", team=TEAM_A, user=USER_A, value="ap-http"))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "executed" and body["acted_by"] == f"{TA}-founder"
    stored = approval_row(TA, "ap-http")
    assert stored["status"] == "executed" and stored["decided_by"] == f"{TA}-founder"
    assert slack.executed() == [{"channel": "#build", "text": "Assigned UNI-158", "blocks": None, "token": TOKEN_A}]
    assert slack.updates[-1] == {
        "channel": "#ops",
        "ts": posted["ts"],
        "text": slack.updates[-1]["text"],
        "token": TOKEN_A,
    }


def test_a_press_with_a_bad_signature_never_reaches_the_handler(client, slack):
    propose(TA, "ap-sig")
    r = post_press(client, press("approval_approve", team=TEAM_A, user=USER_A, value="ap-sig"), secret="wrong")
    assert r.status_code == 403
    assert approval_row(TA, "ap-sig")["status"] == "pending"
    assert slack.updates == []


def test_a_founder_of_another_tenant_changes_nothing(client, slack):
    propose(TA, "ap-cross")

    r = post_press(client, press("approval_approve", team=TEAM_A, user=USER_B, value="ap-cross"))

    assert r.status_code == 200
    assert r.json()["response_type"] == "ephemeral" and "Link your StartupOS account" in r.json()["text"]
    assert approval_row(TA, "ap-cross")["status"] == "pending"
    assert slack.executed() == [] and slack.updates == []


def test_a_press_for_another_tenants_approval_is_invisible_under_rls(client, slack):
    propose(TB, "ap-beta-only")
    # Tenant A's founder, in tenant A's workspace, presses a button carrying tenant B's approval id.
    r = post_press(client, press("approval_approve", team=TEAM_A, user=USER_A, value="ap-beta-only"))

    assert r.status_code == 200 and r.json()["text"] == slack_actions.GONE
    assert approval_row(TB, "ap-beta-only")["status"] == "pending"
    assert slack.executed() == []


# --- decline, twice, and at the same time ---------------------------------------------------------


def test_decline_records_the_decision_and_nothing_executes(client, slack):
    propose(TA, "ap-decline")

    r = post_press(client, press("approval_decline", team=TEAM_A, user=USER_A, value="ap-decline"))

    stored = approval_row(TA, "ap-decline")
    assert r.json()["status"] == "declined"
    assert stored["status"] == "declined" and stored["decline_reason"] == slack_actions.DECLINE_REASON
    assert stored["decided_by"] == f"{TA}-founder" and stored["result"] is None
    assert slack.executed() == []
    assert "Declined" in slack.updates[-1]["text"]

    # …and pressing Approve afterwards still executes nothing.
    again = post_press(client, press("approval_approve", team=TEAM_A, user=USER_A, value="ap-decline"))
    assert again.json()["status"] == "declined" and again.json()["decided"] is False
    assert slack.executed() == []
    assert len(slack.updates) == 2  # the second press re-renders rather than going silent


def test_a_double_click_decides_and_executes_once(workspaces, slack):
    """Two presses, two connections, at the same time: `approvals.decide` takes the row FOR UPDATE, so one wins."""
    propose(TA, "ap-race")
    slack.slow = True
    payload = press("approval_approve", team=TEAM_A, user=USER_A, value="ap-race")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(slack_actions.handle_action, dict(payload), conn_factory=conn_factory) for _ in range(2)]
        results = [f.result(timeout=30) for f in futures]

    assert sorted(r["decided"] for r in results) == [False, True]  # decided exactly once
    assert [r["status"] for r in results] == ["executed", "executed"]  # both presses see the same outcome
    assert len(slack.executed()) == 1  # and the work happened exactly once
    assert len(slack.updates) == 2  # each press re-rendered the message
    assert approval_row(TA, "ap-race")["status"] == "executed"


def test_an_execution_failure_is_recorded_and_shown_in_the_message(workspaces, slack, monkeypatch):
    propose(TA, "ap-boom")

    def boom(inp, *, client=None, token=None):
        if inp["channel"] == "#build":
            raise slack_executor.SlackError("channel_not_found")
        return slack.post(inp, client=client, token=token)

    monkeypatch.setattr(slack_executor, "post_message", boom)
    out = slack_actions.handle_action(
        press("approval_approve", team=TEAM_A, user=USER_A, value="ap-boom"), conn_factory=conn_factory
    )

    stored = approval_row(TA, "ap-boom")
    assert out["decided"] is True and stored["status"] == "failed"  # decided, not still pending
    assert stored["decided_by"] == f"{TA}-founder"
    assert "channel_not_found" in stored["result"]["error"]
    assert "execution failed" in slack.updates[-1]["text"] or "failed" in slack.updates[-1]["text"].lower()


def test_an_approval_proposed_before_slack_was_connected_still_decides(conn, workspaces, slack):
    conn.execute("UPDATE tenants SET pulse_channel = 'web' WHERE id = %s", (TA,))
    conn.commit()
    propose(TA, "ap-late")
    with app_conn_for(TA) as c:
        assert slack_actions.approval_message(c, TA, "ap-late") is None
    conn.execute("UPDATE tenants SET pulse_channel = 'slack' WHERE id = %s", (TA,))
    conn.commit()

    out = slack_actions.handle_action(
        press("approval_approve", team=TEAM_A, user=USER_A, value="ap-late"), conn_factory=conn_factory
    )

    assert approval_row(TA, "ap-late")["status"] == "executed"
    assert out["message"]["updated"] is False and slack.updates == []
    assert len(slack.executed()) == 1  # the decision and the work happened; only the edit had no target


def test_each_workspace_decides_with_its_own_token(workspaces, slack):
    propose(TA, "ap-a")
    propose(TB, "ap-b")

    slack_actions.handle_action(
        press("approval_approve", team=TEAM_A, user=USER_A, value="ap-a"), conn_factory=conn_factory
    )
    slack_actions.handle_action(
        press("approval_approve", team=TEAM_B, user=USER_B, value="ap-b"), conn_factory=conn_factory
    )

    assert [p["token"] for p in slack.executed()] == [TOKEN_A, TOKEN_B]
    assert {u["token"] for u in slack.updates} == {TOKEN_A, TOKEN_B}
    assert approval_row(TA, "ap-a")["decided_by"] == f"{TA}-founder"
    assert approval_row(TB, "ap-b")["decided_by"] == f"{TB}-founder"
