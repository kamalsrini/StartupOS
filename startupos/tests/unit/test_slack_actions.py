"""Track B — daemon/slack_actions.py: the Approve/Decline buttons (CONTRACTS.md Sprint 3b, Track B).

Everything the contract promises about a button press is here: the identity check comes first, the decision goes
through the approval gate exactly once, only an approval reaches an executor, the original message is replaced in
place so the buttons cannot be pressed twice, and no failure below (a stranger, a missing message, a dead Slack,
a failing executor) ever leaves the approval in an unclear state.

Uses the scratch Postgres (the `conn` fixture) because the gate, the ledger and the idempotency ARE the database.
No network: both Slack writes (chat.postMessage from delivery, chat.update from here) are recorders.
"""

from __future__ import annotations

import base64
import os
from contextlib import contextmanager

import pytest

from common import secrets
from common.models import Approval, Exec
from daemon import approvals, delivery, slack_actions
from daemon.executors import slack as slack_executor

TA = "act-a"  # the tenant whose workspace is installed
TB = "act-b"  # a second company, with its own Slack user
TEAM_A = "T-ACT-A"
USER_A_SLACK, USER_B_SLACK = "U-ACT-A", "U-ACT-B"
TOKEN_A = "xoxb-act-a"  # noqa: S105 - test fixture


class Poster:
    """chat.postMessage: what delivery hands Slack when an approval is proposed (and what an executor posts)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    def __call__(self, inp, *, client=None, token=None):
        self.calls.append({"inp": inp, "token": token})
        if not token:  # what the real executor does: no credential for this tenant, no post
            raise slack_executor.SlackError("no Slack credential for this tenant")
        if self.fail:
            raise slack_executor.SlackError("channel_not_found")
        return {
            "text": "Posted",
            "url": "https://slack/x",
            "ts": f"17254000{len(self.calls):02d}.000100",
            "channel": inp["channel"],
        }


class Updater:
    """chat.update: the in-place edit that replaces the buttons with the outcome."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.fail = False

    def __call__(self, inp, *, client=None, token=None):
        self.calls.append({"inp": inp, "token": token})
        if self.fail:
            raise slack_executor.SlackError("message_not_found")
        return {"text": "Updated", "ts": inp["ts"], "channel": inp["channel"]}

    @property
    def last_blocks(self) -> list[dict]:
        return self.calls[-1]["inp"]["blocks"]

    @property
    def last_text(self) -> str:
        """Every piece of text in the re-rendered message — sections and the context line that carries the outcome."""
        out = []
        for b in self.last_blocks:
            if isinstance(b.get("text"), dict):
                out.append(b["text"].get("text", ""))
            for element in b.get("elements") or []:
                if isinstance(element, dict) and isinstance(element.get("text"), str):
                    out.append(element["text"])
        return "\n".join(out)


@pytest.fixture()
def slack(monkeypatch, conn):
    """Both Slack write paths, recorded. The executor module is the only place either one lives."""
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    poster, updater = Poster(), Updater()
    monkeypatch.setattr(slack_executor, "post_message", poster)
    monkeypatch.setattr(slack_executor, "update_message", updater)
    yield {"post": poster, "update": updater}
    # Leave the scratch database as we found it: other suites count installations across all tenants.
    for tenant_id in (TA, TB):
        for table in ("deliveries", "approvals", "connections", "tenant_secrets", "users", "runs", "brain_docs"):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))
        conn.execute("DELETE FROM slack_installations WHERE tenant_id = %s", (tenant_id,))
        conn.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
    conn.commit()


def factory(conn):
    """A conn_factory over one open connection: `handle_action` never decides how to reach the database."""

    @contextmanager
    def _factory(tenant_id: str):
        yield conn

    return _factory


def seed_tenant(conn, tenant_id, *, team_id=None, slack_user=None, token=TOKEN_A, channel="#ops", mode="slack"):
    """A tenant with a Slack channel, its own bot token, one linked user and (optionally) an installed workspace."""
    conn.execute(
        """INSERT INTO tenants (id, name, pulse_channel, slack_channel) VALUES (%s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET pulse_channel = EXCLUDED.pulse_channel,
                                          slack_channel = EXCLUDED.slack_channel""",
        (tenant_id, tenant_id, mode, channel),
    )
    for table in ("deliveries", "approvals", "connections", "tenant_secrets", "users"):
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM slack_installations WHERE tenant_id = %s OR team_id = %s", (tenant_id, team_id or ""))
    user_id = f"{tenant_id}-owner"
    conn.execute(
        """INSERT INTO users (id, tenant_id, email, name, slack_user_id) VALUES (%s, %s, %s, %s, %s)""",
        (user_id, tenant_id, f"owner@{tenant_id}.test", "Owner", slack_user),
    )
    if token:
        secrets.put(conn, tenant_id, "slack_bot_token", token)
        conn.execute(
            """INSERT INTO connections (id, tenant_id, source, secret_ref)
               VALUES (%s, %s, 'slack', 'kv:slack_bot_token')
               ON CONFLICT (tenant_id, source) DO UPDATE SET secret_ref = EXCLUDED.secret_ref""",
            (f"{tenant_id}-slack", tenant_id),
        )
    if team_id:
        conn.execute(
            """INSERT INTO slack_installations (tenant_id, team_id, team_name, bot_user_id, default_channel)
               VALUES (%s, %s, %s, 'U0BOT', %s)""",
            (tenant_id, team_id, tenant_id, channel),
        )
    return user_id


def propose(conn, tenant_id=TA, approval_id="ap-1", *, exec_=None, type_="issue", target="UNI-158"):
    return approvals.propose(
        conn,
        Approval(
            id=approval_id,
            tenant_id=tenant_id,
            module="build",
            type=type_,
            target=target,
            preview="Assign UNI-158 to Alexey",
            exec=exec_,
        ),
    )


SLACK_EXEC = Exec(server="Slack", tool="post_message", input={"channel": "#build", "text": "Assigned UNI-158"})


def press(payload_action, *, team=TEAM_A, user=USER_A_SLACK, value="ap-1"):
    return {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "actions": [{"action_id": payload_action, "value": value, "type": "button"}],
    }


def row(conn, approval_id="ap-1"):
    return dict(conn.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,)).fetchone())


# --- proposing posts the buttons ------------------------------------------------------------------


def test_proposing_an_approval_posts_it_with_buttons_and_records_the_ts(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn)

    assert len(slack["post"].calls) == 1
    blocks = slack["post"].calls[0]["inp"]["blocks"]
    actions = [b for b in blocks if b["type"] == "actions"][0]
    assert [e["action_id"] for e in actions["elements"]] == ["approval_approve", "approval_decline"]
    assert {e["value"] for e in actions["elements"]} == {"ap-1"}
    assert slack["post"].calls[0]["token"] == TOKEN_A  # the tenant's OWN bot token, never an operator's

    ledger = dict(conn.execute("SELECT * FROM deliveries WHERE ref = 'approval:ap-1'").fetchone())
    assert ledger["status"] == "sent" and ledger["ts"] and ledger["channel"] == "#ops"
    assert slack_actions.approval_message(conn, TA, "ap-1") == {"channel": "#ops", "ts": ledger["ts"]}


def test_proposing_the_same_approval_twice_posts_once(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn)
    propose(conn)
    assert len(slack["post"].calls) == 1


def test_a_web_only_tenant_proposes_without_posting(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK, mode="web")
    propose(conn)
    assert slack["post"].calls == []
    assert row(conn)["status"] == "pending"


# --- approve --------------------------------------------------------------------------------------


def test_approve_runs_the_gate_then_the_executor_with_the_tenants_own_credential(conn, slack):
    user_id = seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)

    out = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))

    assert out["status"] == "executed" and out["decided"] is True
    stored = row(conn)
    assert stored["status"] == "executed"
    assert stored["decided_by"] == user_id and stored["decided_at"] is not None  # the MAPPED user, not the Slack id
    assert out["acted_by"] == user_id
    executed = slack["post"].calls[1]  # [0] was the approval message itself
    assert executed["inp"]["channel"] == "#build" and executed["token"] == TOKEN_A


def test_approve_updates_the_original_message_in_place_and_drops_the_buttons(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)
    ts = dict(conn.execute("SELECT ts FROM deliveries WHERE ref = 'approval:ap-1'").fetchone())["ts"]

    out = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))

    update = slack["update"].calls[-1]
    assert update["inp"]["channel"] == "#ops" and update["inp"]["ts"] == ts and update["token"] == TOKEN_A
    assert not [b for b in slack["update"].last_blocks if b["type"] == "actions"]
    assert update["inp"]["text"]  # the notification fallback describes the new state
    assert out["message"]["updated"] is True


def test_a_record_only_approval_is_executed_as_a_record(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn)  # no exec — deciding it IS the outcome

    out = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))

    assert out["status"] == "executed"
    assert len(slack["post"].calls) == 1  # nothing was carried out anywhere
    assert "Recorded" in (row(conn)["result"] or {}).get("text", "")


# --- decline --------------------------------------------------------------------------------------


def test_decline_records_the_decision_and_never_reaches_an_executor(conn, slack):
    user_id = seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)

    out = slack_actions.handle_action(press("approval_decline"), conn_factory=factory(conn))

    stored = row(conn)
    assert stored["status"] == "declined" and stored["decided_by"] == user_id
    assert stored["decline_reason"] == slack_actions.DECLINE_REASON
    assert stored["result"] is None
    assert len(slack["post"].calls) == 1  # the approval message only: the executor never ran
    assert out["status"] == "declined"
    assert "Declined" in slack["update"].last_text or "Declined" in slack["update"].calls[-1]["inp"]["text"]
    decisions = conn.execute(
        "SELECT content FROM brain_docs WHERE tenant_id = %s AND path = 'decisions.md'", (TA,)
    ).fetchone()
    assert decisions and slack_actions.DECLINE_REASON in decisions["content"]


def test_a_declined_approval_is_never_executed_by_a_later_press(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)
    slack_actions.handle_action(press("approval_decline"), conn_factory=factory(conn))

    out = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))

    assert out["status"] == "declined" and out["decided"] is False
    assert row(conn)["status"] == "declined"
    assert len(slack["post"].calls) == 1


# --- pressing twice -------------------------------------------------------------------------------


def test_a_second_press_is_a_harmless_no_op_that_re_renders(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)

    first = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))
    second = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))

    assert first["decided"] is True and second["decided"] is False
    assert first["status"] == second["status"] == "executed"
    assert len([c for c in slack["post"].calls if c["inp"]["channel"] == "#build"]) == 1  # executed ONCE
    assert len(slack["update"].calls) == 2  # but the message is re-rendered, so the press is never silent


# --- who is allowed to press ----------------------------------------------------------------------


def test_an_unlinked_slack_user_changes_nothing_and_is_told_to_link(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)

    out = slack_actions.handle_action(press("approval_approve", user="U-STRANGER"), conn_factory=factory(conn))

    assert out["response_type"] == "ephemeral" and "Link your StartupOS account" in out["text"]
    assert row(conn)["status"] == "pending"
    assert slack["update"].calls == [] and len(slack["post"].calls) == 1


def test_a_user_of_another_tenant_cannot_decide(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    seed_tenant(conn, TB, team_id="T-ACT-B", slack_user=USER_B_SLACK, token="xoxb-act-b")
    propose(conn, exec_=SLACK_EXEC)

    # Tenant B's user presses the button in tenant A's workspace: mapped, but to the wrong company.
    out = slack_actions.handle_action(press("approval_approve", user=USER_B_SLACK), conn_factory=factory(conn))

    assert out["response_type"] == "ephemeral" and "Link your StartupOS account" in out["text"]
    assert row(conn)["status"] == "pending"
    assert slack["update"].calls == []


def test_an_unknown_or_revoked_workspace_is_told_it_is_not_connected(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)

    unknown = slack_actions.handle_action(press("approval_approve", team="T-NOBODY"), conn_factory=factory(conn))
    assert unknown["text"] == slack_actions.NOT_CONNECTED

    conn.execute("UPDATE slack_installations SET revoked_at = now() WHERE team_id = %s", (TEAM_A,))
    revoked = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))
    assert revoked["text"] == slack_actions.NOT_CONNECTED
    assert row(conn)["status"] == "pending"


def test_an_approval_belonging_to_another_tenant_is_gone(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    seed_tenant(conn, TB, token="xoxb-act-b", mode="web")
    propose(conn, TB, "ap-other", exec_=SLACK_EXEC)

    out = slack_actions.handle_action(press("approval_approve", value="ap-other"), conn_factory=factory(conn))

    assert out["text"] == slack_actions.GONE
    assert row(conn, "ap-other")["status"] == "pending"


def test_an_approval_that_does_not_exist_is_gone(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    out = slack_actions.handle_action(press("approval_approve", value="nope"), conn_factory=factory(conn))
    assert out["text"] == slack_actions.GONE


# --- failure paths --------------------------------------------------------------------------------


def test_an_execution_failure_leaves_the_approval_decided_and_says_so_in_the_message(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)
    slack["post"].fail = True  # the executor's own Slack post blows up

    out = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))

    stored = row(conn)
    assert stored["status"] == "failed"  # decided: it is no longer pending, and it is not silently approved
    assert stored["decided_by"] and stored["decided_at"] is not None
    assert "channel_not_found" in stored["result"]["error"]
    assert "channel_not_found" in out["error"]
    assert "execution failed" in slack["update"].last_text
    assert not [b for b in slack["update"].last_blocks if b["type"] == "actions"]  # and the buttons are gone


def test_an_approval_with_no_delivered_message_still_decides(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK, mode="web")  # proposed while Slack was off
    propose(conn, exec_=SLACK_EXEC)
    assert slack_actions.approval_message(conn, TA, "ap-1") is None
    conn.execute("UPDATE tenants SET pulse_channel = 'slack' WHERE id = %s", (TA,))  # …connected afterwards

    out = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))

    assert row(conn)["status"] == "executed"
    assert out["message"] == {"updated": False, "reason": "no delivered message for this approval"}
    assert slack["update"].calls == []


def test_a_failing_chat_update_does_not_undo_the_decision(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)
    slack["update"].fail = True

    out = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))

    assert row(conn)["status"] == "executed"
    assert out["message"]["updated"] is False and "message_not_found" in out["message"]["error"]


def test_no_slack_credential_means_no_update_but_still_a_decision(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)
    conn.execute("DELETE FROM connections WHERE tenant_id = %s AND source = 'slack'", (TA,))

    out = slack_actions.handle_action(press("approval_approve"), conn_factory=factory(conn))

    assert row(conn)["status"] == "failed"  # the executor has no credential either — recorded, not crashed
    assert out["message"]["updated"] is False
    assert slack["update"].calls == []


# --- the router seam ------------------------------------------------------------------------------


def test_an_action_id_we_do_not_own_raises_unknown_action_before_any_io(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)

    for payload in (press("cockpit_snooze"), {"type": "block_actions", "actions": []}, {}):
        with pytest.raises(slack_actions.UnknownAction):
            slack_actions.handle_action(payload, conn_factory=factory(conn))

    with pytest.raises(slack_actions.UnknownAction):  # a button with no approval id is not a decision either
        slack_actions.handle_action(press("approval_approve", value=""), conn_factory=factory(conn))
    assert row(conn)["status"] == "pending"


def test_handle_action_never_opens_its_own_connection(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    propose(conn, exec_=SLACK_EXEC)
    with pytest.raises(RuntimeError):
        slack_actions.handle_action(press("approval_approve"), conn_factory=None)


def test_the_stub_contract_the_router_relies_on_is_unchanged(conn):
    assert slack_actions.KNOWN_ACTIONS == {"approval_approve", "approval_decline"}
    assert slack_actions.action_ids(press("approval_approve")) == ["approval_approve"]
    assert issubclass(slack_actions.UnknownAction, Exception)


# --- the executor's chat.update (the only Slack write path) ---------------------------------------


def test_update_message_requires_channel_ts_and_text_and_checks_ok():
    class Client:
        def __init__(self, ok=True):
            self.ok, self.kwargs = ok, None

        def chat_update(self, **kwargs):
            self.kwargs = kwargs
            return {"ok": self.ok, "error": "message_not_found", "ts": kwargs["ts"], "channel": kwargs["channel"]}

    for bad in ({"ts": "1", "text": "x"}, {"channel": "#c", "text": "x"}, {"channel": "#c", "ts": "1"}):
        with pytest.raises(slack_executor.SlackError):
            slack_executor.update_message(bad, client=Client())

    client = Client()
    out = slack_executor.update_message(
        {"channel": "#c", "ts": "1.2", "text": "done", "blocks": [{"type": "section"}]}, client=client
    )
    assert client.kwargs == {"channel": "#c", "ts": "1.2", "text": "done", "blocks": [{"type": "section"}]}
    assert out["ts"] == "1.2" and out["channel"] == "#c"

    with pytest.raises(slack_executor.SlackError):
        slack_executor.update_message({"channel": "#c", "ts": "1.2", "text": "x"}, client=Client(ok=False))


def test_deliver_approval_takes_a_model_a_row_or_a_dict(conn, slack):
    seed_tenant(conn, TA, team_id=TEAM_A, slack_user=USER_A_SLACK)
    stored = propose(conn, approval_id="ap-model")
    conn.execute("DELETE FROM deliveries WHERE tenant_id = %s", (TA,))

    assert delivery.deliver_approval(conn, TA, stored)["ref"] == "approval:ap-model"
    conn.execute("DELETE FROM deliveries WHERE tenant_id = %s", (TA,))
    assert delivery.deliver_approval(conn, TA, row(conn, "ap-model"))["ref"] == "approval:ap-model"
    conn.execute("DELETE FROM deliveries WHERE tenant_id = %s", (TA,))
    out = delivery.deliver_approval(conn, TA, {"id": "ap-model"}, decided="declined")
    assert out["status"] == "sent"
    assert not [b for b in slack["post"].calls[-1]["inp"]["blocks"] if b["type"] == "actions"]
