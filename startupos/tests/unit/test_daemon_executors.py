"""daemon.executors — status re-check, allow-list, Linear/Slack mapping with fakes."""

from __future__ import annotations

import json

import pytest
from track_b_fakes import TENANT, FakeLinearGraphQL, FakeSlackClient, reset_tenant

from common.models import Approval, Decision, Exec
from daemon import approvals, executors
from daemon.executors import linear, slack

# --- pure ---------------------------------------------------------------------------------------------


def test_allow_list_is_exactly_linear_save_issue_and_slack_post_message():
    assert set(executors.ALLOW_LIST) == {("Linear", "save_issue"), ("Slack", "post_message")}
    assert executors.is_allowed("Linear", "save_issue")
    assert not executors.is_allowed("Brex", "pay_bill")
    assert not executors.is_allowed("Linear", "delete_issue")
    assert not executors.is_allowed(None, None)


def test_linear_save_issue_maps_mcp_input_to_graphql():
    gql = FakeLinearGraphQL()
    out = linear.save_issue(
        {
            "id": "UNI-158",
            "assignee": "Alexey",
            "dueDate": "2026-09-10",
            "priority": 2,
            "state": "In Progress",
            "project": "Sentinel",
            "addLabels": ["Bug"],
            "blockedBy": ["UNI-100"],
            "duplicateOf": "UNI-101",
            "title": "t",
            "description": "d",
        },
        gql=gql,
    )
    assert out["url"] == "https://linear.app/unitone/issue/UNI-158" and out["text"].startswith("Updated UNI-158")
    update = next(v for q, v in gql.mutations if "issueUpdate" in q)
    assert update["id"] == "uuid-uni-158"
    assert update["input"] == {
        "title": "t",
        "description": "d",
        "priority": 2,
        "dueDate": "2026-09-10",
        "assigneeId": "user-uuid-alexey",
        "stateId": "state-uuid",
        "projectId": "project-uuid",
        "addedLabelIds": ["label-bug"],
    }
    relations = [v["input"] for q, v in gql.mutations if "issueRelationCreate" in q]
    assert {"issueId": "uuid-uni-100", "relatedIssueId": "uuid-uni-158", "type": "blocks"} in relations
    assert {"issueId": "uuid-uni-158", "relatedIssueId": "uuid-uni-101", "type": "duplicate"} in relations


def test_linear_create_when_no_id():
    gql = FakeLinearGraphQL()
    out = linear.save_issue({"team": "UNI", "title": "New thing", "priority": 3}, gql=gql)
    create = next(v for q, v in gql.mutations if "issueCreate" in q)
    assert create["input"] == {"title": "New thing", "priority": 3, "teamId": "team-uuid"}
    assert out["url"].endswith("UNI-999")
    with pytest.raises(linear.LinearError):
        linear.save_issue({"title": "no team"}, gql=gql)


def test_linear_unknown_assignee_fails_loudly():
    with pytest.raises(linear.LinearError):
        linear.save_issue({"id": "UNI-1", "assignee": "Nobody"}, gql=FakeLinearGraphQL())


def test_slack_post_message():
    c = FakeSlackClient()
    out = slack.post_message({"channel": "C0789", "text": "hi"}, client=c)
    assert c.posted == [{"channel": "C0789", "text": "hi"}]
    assert out["url"].startswith("https://unitone.slack.com/archives/C0789/")
    with pytest.raises(slack.SlackError):
        slack.post_message({"channel": "C0789"}, client=c)


# --- with Postgres ------------------------------------------------------------------------------------


def _seed(conn, aid: str, exec_: Exec | None = None, status: str = "pending") -> None:
    approvals.propose(
        conn, Approval(id=aid, tenant_id=TENANT, module="build", type="t", target="x", preview="p", exec=exec_)
    )
    if status != "pending":
        conn.execute("UPDATE approvals SET status = %s WHERE id = %s", (status, aid))


@pytest.mark.functional
@pytest.mark.parametrize("status", ["pending", "declined", "executed", "failed"])
def test_executor_refuses_rows_not_approved(conn, monkeypatch, status):
    reset_tenant(conn)
    gql = FakeLinearGraphQL()
    monkeypatch.setattr(linear, "_graphql", gql)
    _seed(conn, "a1", Exec(server="Linear", tool="save_issue", input={"id": "UNI-1", "assignee": "Alexey"}), status)
    with pytest.raises(executors.NotApproved):
        executors.run_approved(conn, "a1")
    assert gql.mutations == []
    assert conn.execute("SELECT status FROM approvals WHERE id = 'a1'").fetchone()["status"] == status


@pytest.mark.functional
def test_executor_refuses_brex_even_when_approved(conn, monkeypatch):
    reset_tenant(conn)
    # Defence in depth: the DB CHECK constraint rejects a Brex exec before any executor can see it
    # (PE review 2026-09-04), and the allow-list refuses it in code as well.
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """INSERT INTO approvals (id, tenant_id, module, type, target, preview, exec, status)
               VALUES ('pay-1', %s, 'finance', 'Brex · pay', 'Vendor', 'pay', %s::jsonb, 'approved')""",
            (TENANT, json.dumps({"server": "Brex", "tool": "pay_bill", "input": {"bill_id": "b1"}})),
        )
    conn.rollback()
    assert not executors.is_allowed("Brex", "pay_bill")


@pytest.mark.functional
def test_executor_runs_approved_linear_and_slack(conn, monkeypatch):
    reset_tenant(conn)
    gql, sc = FakeLinearGraphQL(), FakeSlackClient()
    monkeypatch.setattr(linear, "_graphql", gql)
    monkeypatch.setattr(slack, "_client_factory", lambda: sc)
    _seed(
        conn,
        "a1",
        Exec(
            server="Linear", tool="save_issue", input={"id": "UNI-158", "assignee": "Alexey", "dueDate": "2026-09-10"}
        ),
    )
    _seed(conn, "n1", Exec(server="Slack", tool="post_message", input={"channel": "C1", "text": "nudge"}))
    _seed(conn, "r1", None)  # record-only
    for aid in ("a1", "n1", "r1"):
        approvals.decide(conn, aid, Decision(decision="approve"))

    done = executors.run_all_approved(conn, TENANT)
    by_id = {r["id"]: r for r in done}
    assert by_id["a1"]["status"] == "executed" and by_id["a1"]["result"]["url"].endswith("UNI-158")
    assert by_id["n1"]["status"] == "executed" and sc.posted[0]["text"] == "nudge"
    assert by_id["r1"]["status"] == "executed" and by_id["r1"]["result"]["url"] is None
    assert len(gql.mutations) == 1
    # second pass finds nothing approved; a re-run of an executed id is refused
    assert executors.run_all_approved(conn, TENANT) == []
    with pytest.raises(executors.NotApproved):
        executors.run_approved(conn, "a1")


@pytest.mark.functional
def test_executor_failure_marks_failed_not_crash(conn, monkeypatch):
    reset_tenant(conn)

    def broken(q, v):
        raise linear.LinearError("issue not found: UNI-404")

    monkeypatch.setattr(linear, "_graphql", broken)
    _seed(conn, "a1", Exec(server="Linear", tool="save_issue", input={"id": "UNI-404", "assignee": "Alexey"}))
    approvals.decide(conn, "a1", Decision(decision="approve"))
    row = executors.run_approved(conn, "a1")
    assert (
        row["status"] == "failed" and row["result"]["code"] == "executor_error" and "UNI-404" in row["result"]["error"]
    )


# --- per-tenant credentials (PE review, Sprint 3a) ----------------------------------------------------


def test_real_transports_never_read_the_environment(monkeypatch):
    """Without a per-tenant key the real Linear/Slack transports refuse — no fallback to LINEAR_API_KEY/SLACK_BOT_TOKEN."""
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_OPERATOR")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-operator")
    calls: list[dict] = []

    def never(*_a, **_kw):
        raise AssertionError("the wire must not be touched without a key")

    monkeypatch.setattr(linear.httpx, "post", never)
    with pytest.raises(linear.LinearError, match="no Linear credential"):
        linear.save_issue({"id": "UNI-1", "assignee": "Alexey"})
    assert calls == []
    with pytest.raises(slack.SlackError, match="no Slack credential"):
        slack.post_message({"channel": "C1", "text": "hi"})
    # an explicit key is what reaches the wire
    monkeypatch.setattr(linear.httpx, "post", lambda url, **kw: calls.append(kw) or _Resp())
    with pytest.raises(linear.LinearError, match="issue not found"):
        linear.save_issue({"id": "UNI-1", "assignee": "Alexey"}, api_key="lin_api_TENANT_B")
    assert calls and calls[0]["headers"]["Authorization"] == "lin_api_TENANT_B"


class _Resp:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {"data": {"issue": None}}


@pytest.mark.functional
def test_executor_acts_with_the_approvals_tenants_own_credential(test_dsn, conn, monkeypatch):
    """Tenant B's approved Linear action runs with B's stored key; the operator's env key is never used."""
    import base64
    import os

    from common import secrets
    from common.db import get_conn
    from common.settings import settings

    monkeypatch.setenv("STARTUPOS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_OPERATOR")
    tb = "exec-tenant-b"
    assert tb != settings.tenant_id
    conn.execute("INSERT INTO tenants (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (tb, tb))
    conn.execute("DELETE FROM approvals WHERE tenant_id = %s", (tb,))
    conn.execute("DELETE FROM connections WHERE tenant_id = %s", (tb,))
    conn.commit()
    keys_used: list[str | None] = []

    def transport(query, variables, api_key=None):  # the real transport's contract: no key → refuse
        keys_used.append(api_key)
        if not api_key:
            raise linear.LinearError("no Linear credential for this tenant")
        return FakeLinearGraphQL()(query, variables)

    monkeypatch.setattr(linear, "_default_graphql", transport)
    monkeypatch.setattr(linear, "_graphql", transport)
    with get_conn(test_dsn, tenant_id=tb) as cb:
        approvals.propose(
            cb,
            Approval(
                id="b-1",
                tenant_id=tb,
                module="build",
                type="t",
                target="x",
                preview="p",
                exec=Exec(server="Linear", tool="save_issue", input={"id": "ACM-1", "assignee": "Alexey"}),
            ),
        )
        approvals.decide(cb, "b-1", Decision(decision="approve"))
        # no connection row for B → failed, and the operator's env key was never offered to the transport
        row = executors.run_approved(cb, "b-1")
        assert row["status"] == "failed" and row["result"]["code"] == "executor_error"
        assert "lin_api" not in json.dumps(row["result"])
        assert keys_used == [] or all(k is None for k in keys_used)
        # a pre-3a row pointing B at the operator's env ref is refused, not honoured
        cb.execute(
            "INSERT INTO connections (id, tenant_id, source, secret_ref, status) VALUES (%s, %s, 'linear', 'env:LINEAR_API_KEY', 'connected')",
            (f"{tb}:linear", tb),
        )
        cb.execute("UPDATE approvals SET status = 'approved' WHERE id = 'b-1'")
        row = executors.run_approved(cb, "b-1")
        assert row["status"] == "failed" and "SecretRefForbidden" in row["result"]["error"]
        assert "lin_api" not in json.dumps(row["result"]) and "lin_api_OPERATOR" not in keys_used
        # B's own key (what POST /onboarding/connections {credential} stores) is what the executor acts with
        secrets.put(cb, tb, "linear_api_key", "lin_api_TENANT_B")
        cb.execute("UPDATE connections SET secret_ref = 'kv:linear_api_key' WHERE tenant_id = %s", (tb,))
        cb.execute("UPDATE approvals SET status = 'approved' WHERE id = 'b-1'")
        keys_used.clear()
        row = executors.run_approved(cb, "b-1")
        assert row["status"] == "executed", row["result"]
        assert keys_used and set(keys_used) == {"lin_api_TENANT_B"}
    conn.execute("DELETE FROM approvals WHERE tenant_id = %s", (tb,))
    conn.execute("DELETE FROM connections WHERE tenant_id = %s", (tb,))
    conn.execute("DELETE FROM tenant_secrets WHERE tenant_id = %s", (tb,))
    conn.commit()
