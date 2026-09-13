"""Linear executor: `Linear.save_issue` → GraphQL issueUpdate / issueCreate against https://api.linear.app/graphql.

Accepts the MCP-style save_issue input:
  {id, assignee, dueDate, priority, blockedBy, duplicateOf, state, project, team, title, description, addLabels}
and maps names → ids (assignee by display name via a users lookup, state/project/team/labels by name).

The credential is an argument (`api_key`), resolved per tenant by daemon/executors from the tenant's `connections`
row (common.secrets.credential_for_source); this module never reads the environment. Tests inject `_graphql`.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from typing import Any

import httpx

LINEAR_URL = "https://api.linear.app/graphql"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

GraphQL = Callable[[str, dict[str, Any]], dict[str, Any]]


class LinearError(RuntimeError):
    pass


def _default_graphql(query: str, variables: dict[str, Any], api_key: str | None = None) -> dict[str, Any]:
    if not api_key:
        raise LinearError("no Linear credential for this tenant (connect Linear in onboarding)")
    resp = httpx.post(
        LINEAR_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": api_key, "Content-Type": "application/json"},
        timeout=30.0,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        # GraphQL error messages never include the token; safe to surface.
        raise LinearError("; ".join(e.get("message", "error") for e in body["errors"]))
    return body.get("data") or {}


# Injectable transport: tests set `linear._graphql = fake`.
_graphql: GraphQL = _default_graphql


def _gql(query: str, variables: dict[str, Any] | None = None, gql: GraphQL | None = None) -> dict[str, Any]:
    return (gql or _graphql)(query, variables or {})


def _transport(gql: GraphQL | None, api_key: str | None) -> GraphQL:
    """The transport for one save_issue: an explicit `gql`, else an injected `_graphql` (tests), else the real
    one bound to this tenant's key."""
    if gql is not None:
        return gql
    if _graphql is _default_graphql:
        return functools.partial(_default_graphql, api_key=api_key)
    return _graphql


# --- lookups ---------------------------------------------------------------------------------------


def _first_node(data: dict[str, Any], key: str) -> dict[str, Any] | None:
    nodes = ((data.get(key) or {}).get("nodes")) or []
    return nodes[0] if nodes else None


def resolve_issue(ref: str, gql: GraphQL | None = None) -> dict[str, Any]:
    """Resolve 'UNI-158' or a UUID to {id, identifier, url, team{id}}."""
    data = _gql("query($id: String!) { issue(id: $id) { id identifier url team { id key } } }", {"id": ref}, gql)
    issue = data.get("issue")
    if not issue:
        raise LinearError(f"issue not found: {ref}")
    return issue


def resolve_user(name: str, gql: GraphQL | None = None) -> str:
    if _UUID_RE.match(name):
        return name
    q = """query($n: String!) {
      users(filter: { or: [ { displayName: { eqIgnoreCase: $n } }, { name: { eqIgnoreCase: $n } } ] }, first: 5) {
        nodes { id name displayName active }
      }
    }"""
    node = _first_node(_gql(q, {"n": name}, gql), "users")
    if not node:
        q2 = """query($n: String!) {
          users(filter: { or: [ { name: { containsIgnoreCase: $n } }, { displayName: { containsIgnoreCase: $n } } ] }, first: 5) {
            nodes { id name displayName active }
          }
        }"""
        node = _first_node(_gql(q2, {"n": name}, gql), "users")
    if not node:
        raise LinearError(f"assignee not found: {name}")
    return node["id"]


def resolve_team(ref: str, gql: GraphQL | None = None) -> str:
    if _UUID_RE.match(ref):
        return ref
    q = """query($n: String!) {
      teams(filter: { or: [ { key: { eqIgnoreCase: $n } }, { name: { eqIgnoreCase: $n } } ] }, first: 2) { nodes { id key name } }
    }"""
    node = _first_node(_gql(q, {"n": ref}, gql), "teams")
    if not node:
        raise LinearError(f"team not found: {ref}")
    return node["id"]


def resolve_state(name: str, team_id: str | None, gql: GraphQL | None = None) -> str:
    if _UUID_RE.match(name):
        return name
    if team_id:
        q = """query($n: String!, $t: ID!) {
          workflowStates(filter: { name: { eqIgnoreCase: $n }, team: { id: { eq: $t } } }, first: 2) { nodes { id name } }
        }"""
        node = _first_node(_gql(q, {"n": name, "t": team_id}, gql), "workflowStates")
    else:
        q = "query($n: String!) { workflowStates(filter: { name: { eqIgnoreCase: $n } }, first: 2) { nodes { id name } } }"
        node = _first_node(_gql(q, {"n": name}, gql), "workflowStates")
    if not node:
        raise LinearError(f"state not found: {name}")
    return node["id"]


def resolve_project(name: str, gql: GraphQL | None = None) -> str:
    if _UUID_RE.match(name):
        return name
    q = "query($n: String!) { projects(filter: { name: { eqIgnoreCase: $n } }, first: 2) { nodes { id name } } }"
    node = _first_node(_gql(q, {"n": name}, gql), "projects")
    if not node:
        raise LinearError(f"project not found: {name}")
    return node["id"]


def resolve_labels(names: list[str], gql: GraphQL | None = None) -> list[str]:
    ids: list[str] = []
    for n in names:
        if _UUID_RE.match(n):
            ids.append(n)
            continue
        q = "query($n: String!) { issueLabels(filter: { name: { eqIgnoreCase: $n } }, first: 2) { nodes { id name } } }"
        node = _first_node(_gql(q, {"n": n}, gql), "issueLabels")
        if not node:
            raise LinearError(f"label not found: {n}")
        ids.append(node["id"])
    return ids


# --- mapping ---------------------------------------------------------------------------------------


def build_input(inp: dict[str, Any], team_id: str | None, gql: GraphQL | None = None) -> dict[str, Any]:
    """Map MCP-style fields to IssueUpdateInput / IssueCreateInput. Relations are handled separately."""
    out: dict[str, Any] = {}
    if inp.get("title") is not None:
        out["title"] = inp["title"]
    if inp.get("description") is not None:
        out["description"] = inp["description"]
    if inp.get("priority") is not None:
        out["priority"] = int(inp["priority"])
    if inp.get("dueDate") is not None:
        out["dueDate"] = str(inp["dueDate"])[:10]
    if inp.get("assignee"):
        out["assigneeId"] = resolve_user(str(inp["assignee"]), gql)
    if inp.get("state"):
        out["stateId"] = resolve_state(str(inp["state"]), team_id, gql)
    if inp.get("project"):
        out["projectId"] = resolve_project(str(inp["project"]), gql)
    labels = inp.get("addLabels") or []
    if isinstance(labels, str):
        labels = [labels]
    if labels:
        out["_labelIds"] = resolve_labels([str(x) for x in labels], gql)
    return out


def _relate(issue_uuid: str, other_ref: str, kind: str, gql: GraphQL | None) -> None:
    other = resolve_issue(other_ref, gql)
    if kind == "blockedBy":  # the other issue blocks this one
        variables = {"input": {"issueId": other["id"], "relatedIssueId": issue_uuid, "type": "blocks"}}
    else:  # duplicateOf: this issue duplicates the other
        variables = {"input": {"issueId": issue_uuid, "relatedIssueId": other["id"], "type": "duplicate"}}
    _gql(
        "mutation($input: IssueRelationCreateInput!) { issueRelationCreate(input: $input) { success } }",
        variables,
        gql,
    )


def save_issue(inp: dict[str, Any], *, gql: GraphQL | None = None, api_key: str | None = None) -> dict[str, Any]:
    """Update (when `id` given) or create an issue. Returns {text, url} for approvals.result.

    `api_key` is the acting tenant's Linear key (None → LinearError from the real transport, never an env fallback)."""
    gql = _transport(gql, api_key)
    if inp.get("id"):
        issue = resolve_issue(str(inp["id"]), gql)
        team_id = (issue.get("team") or {}).get("id")
        fields = build_input(inp, team_id, gql)
        label_ids = fields.pop("_labelIds", None)
        if label_ids:
            fields["addedLabelIds"] = label_ids
        if fields:
            data = _gql(
                """mutation($id: String!, $input: IssueUpdateInput!) {
                     issueUpdate(id: $id, input: $input) { success issue { id identifier url } }
                   }""",
                {"id": issue["id"], "input": fields},
                gql,
            )
            res = data.get("issueUpdate") or {}
            if not res.get("success", True):
                raise LinearError(f"issueUpdate failed for {issue['identifier']}")
            issue = res.get("issue") or issue
        for kind in ("blockedBy", "duplicateOf"):
            refs = inp.get(kind)
            if refs:
                for ref in refs if isinstance(refs, list) else [refs]:
                    _relate(issue["id"], str(ref), kind, gql)
        changed = ", ".join(k for k in fields if k != "addedLabelIds") or "relations"
        return {"text": f"Updated {issue.get('identifier', inp['id'])} ({changed})", "url": issue.get("url")}

    # create
    if not inp.get("team"):
        raise LinearError("issueCreate requires `team`")
    if not inp.get("title"):
        raise LinearError("issueCreate requires `title`")
    team_id = resolve_team(str(inp["team"]), gql)
    fields = build_input(inp, team_id, gql)
    label_ids = fields.pop("_labelIds", None)
    if label_ids:
        fields["labelIds"] = label_ids
    fields["teamId"] = team_id
    data = _gql(
        """mutation($input: IssueCreateInput!) {
             issueCreate(input: $input) { success issue { id identifier url } }
           }""",
        {"input": fields},
        gql,
    )
    res = data.get("issueCreate") or {}
    issue = res.get("issue") or {}
    if not res.get("success", True) or not issue:
        raise LinearError("issueCreate failed")
    for kind in ("blockedBy", "duplicateOf"):
        refs = inp.get(kind)
        if refs:
            for ref in refs if isinstance(refs, list) else [refs]:
                _relate(issue["id"], str(ref), kind, gql)
    return {"text": f"Created {issue.get('identifier')}: {inp['title']}", "url": issue.get("url")}
