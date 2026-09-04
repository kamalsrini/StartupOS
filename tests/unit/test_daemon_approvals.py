"""daemon.approvals — propose / decide / list_pending / record-only → decisions.md (needs Postgres)."""

from __future__ import annotations

import pytest
from track_b_fakes import TENANT, reset_tenant

from common.models import Approval, Decision, Exec
from daemon import approvals

pytestmark = pytest.mark.functional


def _approval(aid: str = "assign-uni-158", exec_: Exec | None = None, module: str = "build") -> Approval:
    return Approval(
        id=aid,
        tenant_id=TENANT,
        module=module,
        type="Linear · assign",
        target="UNI-158 → Alexey · due 2026-09-10",
        preview="Assign UNI-158 to Alexey",
        exec=exec_,
    )


def test_propose_is_idempotent_and_lists_pending(conn):
    reset_tenant(conn)
    a = _approval(exec_=Exec(server="Linear", tool="save_issue", input={"id": "UNI-158", "assignee": "Alexey"}))
    first = approvals.propose(conn, a)
    assert first.status == "pending" and first.created_at is not None
    assert first.exec.input["assignee"] == "Alexey"

    a2 = a.model_copy(update={"preview": "changed"})
    second = approvals.propose(conn, a2)
    assert second.preview == "Assign UNI-158 to Alexey"  # existing row wins
    assert conn.execute("SELECT count(*) AS n FROM approvals WHERE tenant_id = %s", (TENANT,)).fetchone()["n"] == 1

    approvals.propose(conn, _approval("ap-bill-1", module="finance"))
    assert [x.id for x in approvals.list_pending(conn, TENANT)] == ["assign-uni-158", "ap-bill-1"]
    assert [x.id for x in approvals.list_pending(conn, TENANT, module="finance")] == ["ap-bill-1"]


def test_decide_approve_with_edited_preview(conn):
    reset_tenant(conn)
    approvals.propose(conn, _approval(exec_=Exec(server="Linear", tool="save_issue", input={"id": "UNI-158"})))
    out = approvals.decide(
        conn,
        "assign-uni-158",
        Decision(decision="approve", edited_preview="Assign to Alexey, due Friday", decided_by="kamal"),
    )
    assert out.status == "approved" and out.decided_by == "kamal" and out.decided_at is not None
    assert out.preview == "Assign to Alexey, due Friday" and out.decline_reason is None
    assert approvals.list_pending(conn, TENANT) == []
    # only pending rows can be decided
    with pytest.raises(ValueError):
        approvals.decide(conn, "assign-uni-158", Decision(decision="decline"))
    with pytest.raises(KeyError):
        approvals.decide(conn, "nope", Decision(decision="approve"))


def test_decline_records_reason_in_decisions_doc(conn):
    reset_tenant(conn)
    approvals.propose(conn, _approval(exec_=Exec(server="Linear", tool="save_issue", input={"id": "UNI-158"})))
    out = approvals.decide(conn, "assign-uni-158", Decision(decision="decline", reason="Alexey is on leave"))
    assert out.status == "declined" and out.decline_reason == "Alexey is on leave"
    doc = conn.execute("SELECT * FROM brain_docs WHERE tenant_id = %s AND path = 'decisions.md'", (TENANT,)).fetchone()
    assert doc is not None and "Declined" in doc["content"] and "Alexey is on leave" in doc["content"]


def test_record_only_approve_appends_to_existing_decisions_doc(conn):
    reset_tenant(conn)
    conn.execute(
        "INSERT INTO brain_docs (tenant_id, path, slice, content, version) VALUES (%s, 'decisions.md', 'decisions', %s, 3)",
        (TENANT, "# Decisions\n- 2026-08-01 · we chose Postgres"),
    )
    approvals.propose(conn, _approval("ap-bill-1", module="finance"))  # exec None → record-only
    out = approvals.decide(conn, "ap-bill-1", Decision(decision="approve", decided_by="owner"))
    assert out.status == "approved" and out.exec is None
    doc = conn.execute("SELECT * FROM brain_docs WHERE tenant_id = %s AND path = 'decisions.md'", (TENANT,)).fetchone()
    lines = doc["content"].splitlines()
    assert lines[0] == "# Decisions" and lines[1] == "- 2026-08-01 · we chose Postgres"
    assert lines[-1].startswith("- ") and "Approved" in lines[-1] and "UNI-158 → Alexey" in lines[-1]
    assert doc["version"] == 4 and doc["source"] == "decision"
