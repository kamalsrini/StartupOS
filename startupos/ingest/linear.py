"""Linear ingest: issues + projects → `issues`, `projects`. Live via GraphQL, or from fixtures (MCP shape)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import psycopg

from ingest import fixtures
from ingest.base import Upserter, UpsertResult, parse_date, parse_ts

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.linear.app/graphql"
KEY_REF = "env:LINEAR_API_KEY"

ISSUES_QUERY = """
query Issues($after: String) {
  issues(first: 100, after: $after, orderBy: updatedAt) {
    nodes {
      id identifier title priority url createdAt updatedAt
      state { name type }
      assignee { name }
      project { name }
      team { name }
      labels { nodes { name } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

PROJECTS_QUERY = """
query Projects($after: String) {
  projects(first: 100, after: $after) {
    nodes {
      id name url targetDate updatedAt
      status { name type }
      lead { name }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

ISSUE_COLUMNS = [
    "id",
    "uuid",
    "title",
    "status",
    "status_type",
    "priority",
    "assignee",
    "project",
    "team",
    "labels",
    "url",
    "created_at",
    "updated_at",
    "raw",
]
PROJECT_COLUMNS = ["id", "name", "status", "target_date", "lead", "url", "updated_at", "raw"]

issues_upserter = Upserter("issues", "linear", ["id"], ISSUE_COLUMNS, json_cols=["raw"], skip_compare=["uuid"])
projects_upserter = Upserter("projects", "linear", ["id"], PROJECT_COLUMNS, json_cols=["raw"])


# --- normalizers -----------------------------------------------------------------


def _name(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, dict):
        return v.get("name") or None
    return str(v) or None


def normalize_issue(raw: dict[str, Any]) -> dict[str, Any]:
    """MCP fixture shape (id = identifier, priority {value,name}, status/statusType strings) → issues row."""
    prio = raw.get("priority")
    if isinstance(prio, dict):
        prio = prio.get("value")
    labels = raw.get("labels") or []
    if isinstance(labels, dict):
        labels = labels.get("nodes") or []
    labels = [_name(label) for label in labels if _name(label)]
    return {
        "id": raw.get("identifier") or raw["id"],
        "uuid": raw.get("uuid") or (raw["id"] if raw.get("identifier") else None),
        "title": raw["title"],
        "status": raw.get("status") if isinstance(raw.get("status"), str) else _name(raw.get("state")),
        "status_type": raw.get("statusType") or (raw.get("state") or {}).get("type"),
        "priority": int(prio) if prio is not None else None,
        "assignee": _name(raw.get("assignee")),
        "project": _name(raw.get("project")),
        "team": _name(raw.get("team")),
        "labels": labels,
        "url": raw.get("url"),
        "created_at": parse_ts(raw.get("createdAt")),
        "updated_at": parse_ts(raw.get("updatedAt")),
        "raw": raw,
    }


def normalize_issue_graphql(node: dict[str, Any]) -> dict[str, Any]:
    """Linear GraphQL node → issues row (routes through normalize_issue)."""
    return normalize_issue(
        {
            "identifier": node["identifier"],
            "id": node["id"],
            "uuid": node["id"],
            "title": node["title"],
            "state": node.get("state"),
            "priority": node.get("priority"),
            "assignee": node.get("assignee"),
            "project": node.get("project"),
            "team": node.get("team"),
            "labels": node.get("labels"),
            "url": node.get("url"),
            "createdAt": node.get("createdAt"),
            "updatedAt": node.get("updatedAt"),
        }
    )


def normalize_project(raw: dict[str, Any]) -> dict[str, Any]:
    status = raw.get("status")
    if isinstance(status, dict):
        status = status.get("name")
    elif status is None and isinstance(raw.get("state"), str):
        status = raw["state"]
    return {
        "id": raw["id"],
        "name": raw["name"],
        "status": status,
        "target_date": parse_date(raw.get("targetDate")),
        "lead": _name(raw.get("lead")),
        "url": raw.get("url"),
        "updated_at": parse_ts(raw.get("updatedAt")),
        "raw": raw,
    }


# --- sources -----------------------------------------------------------------------


def from_fixture(base: Path | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    issues = [normalize_issue(i) for i in fixtures.load("linear_issues.json", base)["issues"]]
    projects = [normalize_project(p) for p in fixtures.load("linear_projects.json", base)["projects"]]
    return issues, projects


def _graphql(client: httpx.Client, api_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    resp = client.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        timeout=30.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise RuntimeError(f"linear graphql error: {payload['errors'][0].get('message', 'unknown')}")
    return payload["data"]


def _paginate(client: httpx.Client, api_key: str, query: str, root: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    after = None
    while True:
        data = _graphql(client, api_key, query, {"after": after})[root]
        nodes.extend(data["nodes"])
        info = data["pageInfo"]
        if not info.get("hasNextPage"):
            return nodes
        after = info["endCursor"]


def fetch_live(api_key: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not api_key:
        raise RuntimeError("linear: no api key")
    with httpx.Client() as client:
        issues = [normalize_issue_graphql(n) for n in _paginate(client, api_key, ISSUES_QUERY, "issues")]
        projects = [normalize_project(n) for n in _paginate(client, api_key, PROJECTS_QUERY, "projects")]
    return issues, projects


def sync(
    conn: psycopg.Connection,
    tenant_id: str,
    *,
    api_key: str | None = None,
    use_fixtures: bool = False,
    fixtures_dir: Path | None = None,
    **_config: Any,
) -> dict[str, UpsertResult]:
    """Upsert issues + projects for `tenant_id`. The key is an argument (resolved by the runner per tenant);
    this module never reads the environment."""
    issues, projects = from_fixture(fixtures_dir) if use_fixtures else fetch_live(api_key or "")
    return {
        "issues": issues_upserter.upsert(conn, tenant_id, issues),
        "projects": projects_upserter.upsert(conn, tenant_id, projects),
    }
