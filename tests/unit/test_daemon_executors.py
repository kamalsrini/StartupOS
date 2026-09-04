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
