"""Fakes and seed helpers for Track B (daemon) tests. No network, no real anthropic/Linear/Slack calls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

TENANT = "unitone"


class FakeAnthropic:
    """Mimics anthropic.Anthropic().messages.create(**kwargs) and records every call."""

    def __init__(self, text: str = "four lines\nthree things", usage: tuple[int, int, int] = (1_000, 8_000, 200)):
        self.text = text
        self.usage = usage
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        tin, cached, out = self.usage
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=tin, cache_read_input_tokens=cached, output_tokens=out),
            model=kwargs.get("model"),
        )


class FakeLinearGraphQL:
    """Callable (query, variables) -> data. Answers the lookups save_issue performs and records mutations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.users = {"alexey": "user-uuid-alexey", "maria": "user-uuid-maria"}

    def __call__(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        q = " ".join(query.split())
        if q.startswith("query") and "issue(id:" in q:
            ident = variables["id"]
            return {
                "issue": {
                    "id": f"uuid-{ident.lower()}",
                    "identifier": ident,
                    "url": f"https://linear.app/unitone/issue/{ident}",
                    "team": {"id": "team-uuid", "key": ident.split("-")[0]},
                }
            }
        if "users(filter" in q:
            uid = self.users.get(variables["n"].lower())
            return {
                "users": {"nodes": [{"id": uid, "name": variables["n"], "displayName": variables["n"]}] if uid else []}
            }
        if "workflowStates(filter" in q:
            return {"workflowStates": {"nodes": [{"id": "state-uuid", "name": variables["n"]}]}}
        if "projects(filter" in q:
            return {"projects": {"nodes": [{"id": "project-uuid", "name": variables["n"]}]}}
        if "teams(filter" in q:
            return {"teams": {"nodes": [{"id": "team-uuid", "key": "UNI", "name": variables["n"]}]}}
        if "issueLabels(filter" in q:
            return {"issueLabels": {"nodes": [{"id": f"label-{variables['n'].lower()}", "name": variables["n"]}]}}
        if "issueUpdate(" in q:
            ident = variables["id"].removeprefix("uuid-").upper()
            return {
                "issueUpdate": {
                    "success": True,
                    "issue": {
                        "id": variables["id"],
                        "identifier": ident,
                        "url": f"https://linear.app/unitone/issue/{ident}",
                    },
                }
            }
        if "issueCreate(" in q:
            return {
                "issueCreate": {
                    "success": True,
                    "issue": {
                        "id": "uuid-uni-999",
                        "identifier": "UNI-999",
                        "url": "https://linear.app/unitone/issue/UNI-999",
                    },
                }
            }
        if "issueRelationCreate(" in q:
            return {"issueRelationCreate": {"success": True}}
        raise AssertionError(f"unexpected GraphQL: {q[:80]}")

    @property
    def mutations(self) -> list[tuple[str, dict[str, Any]]]:
        return [(q, v) for q, v in self.calls if q.lstrip().startswith("mutation")]


class FakeSlackClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802 - slack_sdk naming
        self.posted.append(kwargs)
        return {"ok": True, "ts": "1725400000.000100", "channel": kwargs["channel"]}

    def chat_getPermalink(self, **kwargs: Any) -> dict[str, Any]:  # noqa: N802
        return {"ok": True, "permalink": f"https://unitone.slack.com/archives/{kwargs['channel']}/p1725400000000100"}


# --- seeding -----------------------------------------------------------------------------------------


def reset_tenant(conn, tenant: str = TENANT) -> None:
    """Wipe Track-B-relevant rows for the tenant (FK order matters)."""
    for sql in (
        "DELETE FROM approvals WHERE tenant_id = %s",
        "DELETE FROM runs WHERE tenant_id = %s",
        "DELETE FROM signals WHERE tenant_id = %s",
        "DELETE FROM budgets WHERE tenant_id = %s",
        "DELETE FROM issues WHERE tenant_id = %s",
        "DELETE FROM bills WHERE tenant_id = %s",
        "DELETE FROM brain_docs WHERE tenant_id = %s",
        "DELETE FROM context_packs WHERE tenant_id = %s",
        "DELETE FROM messages WHERE tenant_id = %s",
        "DELETE FROM documents WHERE tenant_id = %s",
    ):
        conn.execute(sql, (tenant,))


def seed_issue(
    conn,
    issue_id: str,
    title: str,
    *,
    priority: int = 2,
    assignee: str | None = None,
    status: str = "Backlog",
    labels: list[str] | None = None,
    project: str | None = None,
    updated_days_ago: int = 0,
    tenant: str = TENANT,
) -> None:
    conn.execute(
        """INSERT INTO issues (tenant_id, id, title, status, status_type, priority, assignee, project, team, labels, url,
                               created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'UNI', %s, %s, now() - interval '30 days', %s)
           ON CONFLICT (tenant_id, id) DO UPDATE SET title = EXCLUDED.title, assignee = EXCLUDED.assignee""",
        (
            tenant,
            issue_id,
            title,
            status,
            "started" if status == "In Progress" else "backlog",
            priority,
            assignee,
            project,
            labels or [],
            f"https://linear.app/unitone/issue/{issue_id}",
            datetime.now(UTC) - timedelta(days=updated_days_ago),
        ),
    )


def seed_signal(
    conn,
    rule_id: str,
    entity_id: str,
    *,
    module: str = "build",
    severity: str = "high",
    kind: str = "click",
    title: str | None = None,
    meta: str = "",
    tenant: str = TENANT,
) -> str:
    sid = f"{rule_id}:{entity_id}"
    conn.execute(
        """INSERT INTO signals (id, tenant_id, module, rule_id, severity, kind, title, meta, entity, entity_id)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'issues', %s)
           ON CONFLICT (tenant_id, id) DO UPDATE SET last_seen_at = now(), resolved_at = NULL""",
        (sid, tenant, module, rule_id, severity, kind, title or f"{entity_id} needs attention", meta, entity_id),
    )
    return sid


def seed_team_doc(conn, content: str, tenant: str = TENANT) -> None:
    conn.execute(
        """INSERT INTO brain_docs (tenant_id, path, slice, content) VALUES (%s, 'team.md', 'team', %s)
           ON CONFLICT (tenant_id, path) DO UPDATE SET content = EXCLUDED.content""",
        (tenant, content),
    )


def seed_bill(
    conn,
    bill_id: str,
    vendor: str,
    amount: str,
    *,
    due_in_days: int,
    payment_status: str = "AWAITING_PAYMENT",
    status: str = "APPROVED",
    tenant: str = TENANT,
) -> None:
    conn.execute(
        """INSERT INTO bills (tenant_id, id, vendor_id, vendor_name, amount, currency, status, payment_status, due_at, invoice_number)
           VALUES (%s, %s, %s, %s, %s, 'USD', %s, %s, %s, %s)
           ON CONFLICT (tenant_id, id) DO UPDATE SET payment_status = EXCLUDED.payment_status, due_at = EXCLUDED.due_at""",
        (
            tenant,
            bill_id,
            f"v-{bill_id}",
            vendor,
            amount,
            status,
            payment_status,
            datetime.now(UTC) + timedelta(days=due_in_days),
            f"INV-{bill_id}",
        ),
    )


def seed_pack(conn, content: str = "# Identity\nUnitOne builds Sentinel.\n", tenant: str = TENANT) -> None:
    from common.ids import content_key

    conn.execute(
        "INSERT INTO context_packs (tenant_id, content, token_estimate, cache_key) VALUES (%s, %s, %s, %s)",
        (tenant, content, len(content) // 4, content_key(content)),
    )
