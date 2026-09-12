"""build.assign_owner — Unassigned High → suggested owner (Tier 1 optional; deterministic v1 = Tier 0).

For each open `build.unassigned_high` signal: read team AORs from brain_docs team.md, pick the owner whose
keywords best match the issue (title, labels, project), and propose an Approval with
exec Linear.save_issue {id, assignee, dueDate=+7d}. Approve → executor assigns in Linear.
"""

from __future__ import annotations

import re
from typing import Any

from common.ids import approval_id
from common.models import Approval, Exec
from daemon import approvals, llm
from daemon.skills.base import Ctx, Skill, approval_exists, open_signals, plus_days, register, signal_exists

NAME = "build.assign_owner"
_SEP = re.compile(r"\s+—\s+|\s+–\s+|\s*:\s*|\s+-\s+|\s*\|\s*")
_HEADERISH = {"name", "role", "owner", "aor", "aors", "area", "areas", "team", "member", "who", "what"}


def parse_aors(team_md: str) -> list[tuple[str, list[str]]]:
    """Extract [(owner, [keywords])] from a Markdown team doc. Tolerates bullets, tables, 'Name: a, b' lines."""
    out: list[tuple[str, list[str]]] = []
    body = team_md
    if body.lstrip().startswith("---"):  # skip YAML front matter — 'updated: 2026-…' is not a person
        end = body.find("\n---", 3)
        body = body[end + 4 :] if end != -1 else body
    for raw in body.splitlines():
        line = raw.strip().strip("-*|#• ").strip()
        if not line or set(line) <= set("-|: "):
            continue
        if "→" in line or "->" in line:  # heuristic form: "fixer, eval, graph → Alexey"
            lhs, _, rhs = re.split(r"→|->", line, maxsplit=1)[0], None, re.split(r"→|->", line, maxsplit=1)[1]
            name = rhs.strip().strip("*_`. ")
            kws = [k.strip().strip("*_`.").lower() for k in re.split(r"[,;/+]+", lhs) if k.strip()]
            kws = [re.sub(r"\(.*?\)", "", k).strip() for k in kws]
            if name and kws:
                out.append((name, [k for k in kws if k]))
            continue
        parts = [p.strip().strip("*_`") for p in _SEP.split(line) if p and p.strip()]
        if len(parts) < 2:
            continue
        name = re.sub(r"\(.*?\)", "", parts[0]).strip().strip("*_` ").strip()
        if not name or len(name.split()) > 3 or name.lower() in _HEADERISH:
            continue
        kws: list[str] = []
        for seg in parts[1:]:
            for kw in re.split(r"[,;/+]+", seg):
                k = kw.strip().strip("*_`.").lower()
                k = re.sub(r"\(.*?\)", "", k).strip()
                if k and k not in _HEADERISH and len(k) < 40:
                    kws.append(k)
        if kws:
            out.append((name, kws))
    return out


def pick_owner(text: str, aors: list[tuple[str, list[str]]]) -> tuple[str, list[str]] | None:
    """Best owner by keyword hits in text; ties keep team.md order. None when the team doc is empty."""
    if not aors:
        return None
    hay = text.lower()
    best: tuple[str, list[str]] | None = None
    best_hits: list[str] = []
    for name, kws in aors:
        hits = [k for k in kws if k in hay and len(k) > 2]
        if len(hits) > len(best_hits):
            best, best_hits = (name, kws), hits
    if best is None:
        return aors[0][0], []
    return best[0], best_hits


def _team_doc(ctx: Ctx) -> str:
    row = (
        ctx["conn"]
        .execute(
            "SELECT content FROM brain_docs WHERE tenant_id = %s AND (path = 'team.md' OR slice = 'team') ORDER BY path LIMIT 1",
            (ctx["tenant_id"],),
        )
        .fetchone()
    )
    return row["content"] if row else ""


def _issue(ctx: Ctx, issue_id: str | None) -> dict[str, Any] | None:
    if not issue_id:
        return None
    row = (
        ctx["conn"]
        .execute("SELECT * FROM issues WHERE tenant_id = %s AND id = %s", (ctx["tenant_id"], issue_id))
        .fetchone()
    )
    return dict(row) if row else None


def run(ctx: Ctx) -> list[Approval]:
    conn, tenant = ctx["conn"], ctx["tenant_id"]
    aors = parse_aors(_team_doc(ctx))
    signals = open_signals(conn, tenant, "build.unassigned_high")
    if not signals:
        return []
    if not aors:
        llm.record_tier0(conn, tenant, NAME, "signal", "skipped: no AORs in brain_docs team.md", status="degraded")
        return []

    proposed: list[Approval] = []
    run_id = llm.record_tier0(conn, tenant, NAME, "signal", f"evaluated {len(signals)} unassigned-high signals")
    for sig in signals:
        issue_id = sig.get("entity_id") or (sig["id"].split(":", 1)[1] if ":" in sig["id"] else None)
        if not issue_id:
            continue
        aid = approval_id("assign", issue_id)
        if approval_exists(conn, aid):
            continue
        issue = _issue(ctx, issue_id) or {}
        text = " ".join(
            str(x)
            for x in [issue.get("title") or sig.get("title"), " ".join(issue.get("labels") or []), issue.get("project")]
            if x
        )
        picked = pick_owner(text, aors)
        if not picked:
            continue
        owner, hits = picked
        due = plus_days(ctx, 7)
        rationale = (
            f"AOR match on {', '.join(hits)}" if hits else "no AOR keyword matched; defaulting to first listed owner"
        )
        title = issue.get("title") or sig.get("title") or issue_id
        approval = Approval(
            id=aid,
            tenant_id=tenant,
            module="build",
            type="Linear · assign",
            target=f"{issue_id} → {owner} · due {due}",
            preview=f'Assign {issue_id} "{title}" to {owner}, due {due}. Rationale: {rationale} (team.md).',
            exec=Exec(server="Linear", tool="save_issue", input={"id": issue_id, "assignee": owner, "dueDate": due}),
            created_by_run=run_id,
            signal_id=signal_exists(conn, sig["id"]),
        )
        proposed.append(approvals.propose(conn, approval))
    return proposed


SKILL = register(
    Skill(
        name=NAME,
        module="build",
        tier=0,
        trigger="signal",
        run=run,
        description="Unassigned High issue → owner proposal from team AORs",
    )
)
