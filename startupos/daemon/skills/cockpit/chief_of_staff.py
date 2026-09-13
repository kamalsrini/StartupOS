"""cockpit.chief_of_staff — the hub agent. Tier 2, 06:30 tenant-local (before the pulse) and on demand.

Inputs are assembled at Tier 0 (context pack, ALL open signals by module, 7 days of events by entity,
signals/lookahead facts, the last five briefs, decisions.md tail) and sent as ONE model call: the prompt in
daemon/prompts/chief_of_staff.md + the pack as the cacheable system block, the inputs as user JSON.

Output (CONTRACTS.md "Chief of Staff"): {brief, risks, asks, proposals, changes_since_last}.
  * risks + asks → signals `cos.risk:<slug>` (kind open, module = the spoke); ones missing from this run resolve
  * proposals → approvals via daemon/approvals; exec must be in the executor allow-list or the proposal is dropped
  * the brief → runs.outcome of the model run (what /cockpit shows as the pulse when present)

Degradation: invalid JSON → one retry → Tier-0 fallback (lookahead headlines) recorded as status='degraded'.
BudgetExhausted / no credentials → same fallback. The Cockpit never shows nothing.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from common.ids import approval_id
from common.models import Approval, Exec
from daemon import approvals, llm
from daemon.skills.base import Ctx, Skill, register, signal_exists, system_prompt
from signals import lookahead

log = logging.getLogger("daemon.skills.chief_of_staff")

NAME = "cockpit.chief_of_staff"
RULE_ID = "cos.risk"
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "chief_of_staff.md"
USER_PROMPT = "Here is the hub state. Write today's Chief of Staff output as strict JSON."
RETRY_NUDGE = "That was not valid JSON. Return valid JSON only — the exact schema, no prose, no code fences."

MODULES = {"sales", "finance", "build", "customers", "marketing", "web", "social", "security", "cockpit"}
SEVERITIES = {"high", "medium", "low"}
ALLOW_LIST = {("Linear", "save_issue"), ("Slack", "post_message")}
EVENTS_WINDOW = timedelta(days=7)
PRIOR_BRIEFS = 5
DECISIONS_TAIL_LINES = 15
MAX_ITEMS = 12


def prompt_text() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


# --- input assembly (Tier 0) ---------------------------------------------------------------------------


def _open_signals_by_module(conn: Any, tenant: str) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """SELECT id, module, rule_id, severity, kind, title, meta, entity_id, first_seen_at
           FROM signals WHERE tenant_id = %s AND resolved_at IS NULL AND rule_id <> %s
           ORDER BY module, severity, id""",
        (tenant, RULE_ID),
    ).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["module"], []).append(
            {
                "id": r["id"],
                "severity": r["severity"],
                "kind": r["kind"],
                "title": r["title"],
                "meta": r["meta"],
                "since": r["first_seen_at"].date().isoformat() if r["first_seen_at"] else None,
            }
        )
    return out


def _events_by_entity(conn: Any, tenant: str, now: Any) -> dict[str, Any]:
    since = now - EVENTS_WINDOW
    counts = conn.execute(
        """SELECT entity, kind, count(*) AS n FROM events
           WHERE tenant_id = %s AND occurred_at >= %s GROUP BY entity, kind ORDER BY entity, kind""",
        (tenant, since),
    ).fetchall()
    busiest = conn.execute(
        """SELECT entity, entity_id, count(*) AS n FROM events
           WHERE tenant_id = %s AND occurred_at >= %s GROUP BY entity, entity_id ORDER BY n DESC, entity, entity_id
           LIMIT 25""",
        (tenant, since),
    ).fetchall()
    # PE review 2026-09-04: `events.kind='created'` means "first seen by StartupOS" (an initial backfill emits
    # one per row), not "created at the source". Name it so the model never reports backfills as new work.
    out: dict[str, Any] = {
        "_note": "counts are StartupOS change-log events; 'first_seen' = row first ingested (backfills inflate it), "
        "'updated' = a field changed at the source. Use issues.created_at for real creation dates."
    }
    for r in counts:
        kind = "first_seen" if r["kind"] == "created" else r["kind"]
        out.setdefault(r["entity"], {"counts": {}, "busiest": []})["counts"][kind] = int(r["n"])
    for r in busiest:
        out.setdefault(r["entity"], {"counts": {}, "busiest": []})["busiest"].append(
            {"id": r["entity_id"], "events": int(r["n"])}
        )
    return out


def prior_briefs(conn: Any, tenant: str, limit: int = PRIOR_BRIEFS) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT started_at, status, outcome FROM runs
           WHERE tenant_id = %s AND skill = %s AND outcome IS NOT NULL AND outcome NOT LIKE 'refused:%%'
             AND outcome NOT LIKE 'error:%%'
           ORDER BY started_at DESC LIMIT %s""",
        (tenant, NAME, limit),
    ).fetchall()
    return [{"at": r["started_at"].isoformat(), "status": r["status"], "brief": r["outcome"]} for r in rows]


def _prior_risks(conn: Any, tenant: str, now: Any) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        """SELECT id, module, severity, title, meta, resolved_at FROM signals
           WHERE tenant_id = %s AND rule_id = %s AND (resolved_at IS NULL OR resolved_at >= %s) ORDER BY id""",
        (tenant, RULE_ID, now - EVENTS_WINDOW),
    ).fetchall()
    out: dict[str, list[dict[str, Any]]] = {"open": [], "resolved_last_7d": []}
    for r in rows:
        item = {"id": r["id"], "module": r["module"], "severity": r["severity"], "title": r["title"], "why": r["meta"]}
        out["open" if r["resolved_at"] is None else "resolved_last_7d"].append(item)
    return out


def _decisions_tail(conn: Any, tenant: str, lines: int = DECISIONS_TAIL_LINES) -> list[str]:
    row = conn.execute(
        "SELECT content FROM brain_docs WHERE tenant_id = %s AND path = 'decisions.md'", (tenant,)
    ).fetchone()
    if not row:
        return []
    body = [ln for ln in row["content"].splitlines() if ln.strip() and not ln.startswith("---")]
    return body[-lines:]


def assemble_inputs(ctx: Ctx) -> dict[str, Any]:
    conn, tenant, now = ctx["conn"], ctx["tenant_id"], ctx["now"]
    inputs = {
        "now": now.isoformat(),
        "trigger": ctx.get("trigger") or "schedule",
        "open_signals_by_module": _open_signals_by_module(conn, tenant),
        "events_last_7d_by_entity": _events_by_entity(conn, tenant, now),
        "lookahead": lookahead.compute(conn, tenant, now),
        "prior_briefs": prior_briefs(conn, tenant),
        "prior_risks": _prior_risks(conn, tenant, now),
        "decisions_tail": _decisions_tail(conn, tenant),
    }
    if ctx.get("question"):
        inputs["question"] = str(ctx["question"])
    return inputs


# --- output validation ---------------------------------------------------------------------------------


class BadOutput(ValueError):
    pass


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def parse_output(text: str) -> dict[str, Any]:
    """Strict: must be one JSON object with the contracted keys. Code fences are the only tolerated noise."""
    cleaned = _FENCE.sub("", text or "").strip()
    try:
        obj = json.loads(cleaned)
    except ValueError as exc:
        raise BadOutput(f"not JSON: {exc}") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("brief"), str) or not obj["brief"].strip():
        raise BadOutput("missing brief")
    for key in ("risks", "asks", "proposals", "changes_since_last"):
        if not isinstance(obj.get(key, []), list):
            raise BadOutput(f"{key} is not a list")
        obj.setdefault(key, [])
    return {
        "brief": obj["brief"].strip(),
        "risks": [r for r in obj["risks"] if isinstance(r, dict) and r.get("title")][:MAX_ITEMS],
        "asks": [a for a in obj["asks"] if isinstance(a, dict) and a.get("title")][:MAX_ITEMS],
        "proposals": [p for p in obj["proposals"] if isinstance(p, dict)][:MAX_ITEMS],
        "changes_since_last": [str(c) for c in obj["changes_since_last"] if c][:MAX_ITEMS],
    }


def slugify(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit].strip("-")


def risk_signal_id(module: str, title: str) -> str:
    return f"{RULE_ID}:{slugify(f'{module}-{title}')}"


# --- writes ------------------------------------------------------------------------------------------


def write_signals(ctx: Ctx, out: dict[str, Any]) -> dict[str, Any]:
    """Upsert cos.risk signals for risks + asks; resolve open cos.risk signals absent from this run."""
    conn, tenant, now = ctx["conn"], ctx["tenant_id"], ctx["now"]
    ids: list[str] = []
    items: list[tuple[str, str, str, str, str]] = []  # (id, module, severity, title, meta)
    for r in out["risks"]:
        module = str(r.get("module") or "cockpit").lower()
        module = module if module in MODULES else "cockpit"
        sev = str(r.get("severity") or "medium").lower()
        sev = sev if sev in SEVERITIES else "medium"
        horizon = r.get("horizon_days")
        meta = f"{horizon}d · {r.get('why') or ''}".strip(" ·") if horizon is not None else str(r.get("why") or "")
        r["signal_id"] = risk_signal_id(module, str(r["title"]))
        items.append((r["signal_id"], module, sev, str(r["title"])[:200], meta[:1000]))
    for a in out["asks"]:
        module = str(a.get("module") or "cockpit").lower()
        module = module if module in MODULES else "cockpit"
        meta = " · ".join(
            s
            for s in (
                f"owner {a.get('owner')}" if a.get("owner") else "",
                f"by {a.get('by')}" if a.get("by") else "",
                str(a.get("why") or ""),
            )
            if s
        )
        a["signal_id"] = risk_signal_id("ask", str(a["title"]))
        items.append((a["signal_id"], module, "medium", str(a["title"])[:200], meta[:1000]))
    for sid, module, sev, title, meta in items:
        if sid in ids:
            continue
        ids.append(sid)
        conn.execute(
            """INSERT INTO signals (id, tenant_id, module, rule_id, severity, kind, title, meta, entity, entity_id,
                                    suggested_skill, href, first_seen_at, last_seen_at, resolved_at)
               VALUES (%s, %s, %s, %s, %s, 'open', %s, %s, 'cos', %s, %s, NULL, %s, %s, NULL)
               ON CONFLICT (tenant_id, id) DO UPDATE SET
                 module = EXCLUDED.module, severity = EXCLUDED.severity, title = EXCLUDED.title, meta = EXCLUDED.meta,
                 last_seen_at = EXCLUDED.last_seen_at, resolved_at = NULL""",
            (sid, tenant, module, RULE_ID, sev, title, meta, sid.split(":", 1)[1], NAME, now, now),
        )
    resolved = conn.execute(
        """UPDATE signals SET resolved_at = %s
           WHERE tenant_id = %s AND rule_id = %s AND resolved_at IS NULL AND NOT (id = ANY(%s))""",
        (now, tenant, RULE_ID, ids),
    ).rowcount
    return {"written": ids, "resolved": resolved}


def validate_exec(raw: Any) -> Exec | None:
    """Allow-list gate: Linear.save_issue / Slack.post_message only. Raises BadOutput for anything else."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BadOutput("exec is not an object")
    server, tool = str(raw.get("server") or ""), str(raw.get("tool") or "")
    if (server, tool) not in ALLOW_LIST:
        raise BadOutput(f"exec {server}.{tool} is outside the allow-list")
    inp = raw.get("input")
    if not isinstance(inp, dict):
        raise BadOutput("exec.input is not an object")
    return Exec(server=server, tool=tool, input=inp)  # type: ignore[arg-type]


def write_proposals(ctx: Ctx, out: dict[str, Any], run_id: str | None) -> list[Approval]:
    conn, tenant = ctx["conn"], ctx["tenant_id"]
    proposed: list[Approval] = []
    for p in out["proposals"]:
        try:
            exec_ = validate_exec(p.get("exec"))
        except BadOutput as exc:
            log.warning("chief_of_staff dropped proposal %r: %s", p.get("target"), exc)
            continue
        module = str(p.get("module") or "cockpit").lower()
        module = module if module in MODULES else "cockpit"
        target = str(p.get("target") or p.get("type") or "").strip()
        preview = str(p.get("preview") or "").strip()
        if not target or not preview:
            log.warning("chief_of_staff dropped proposal without target/preview: %r", p)
            continue
        approval = Approval(
            id=approval_id("cos", f"{module}-{target}"),
            tenant_id=tenant,
            module=module,
            type=str(p.get("type") or ("Decision" if exec_ is None else f"{exec_.server} · {exec_.tool}"))[:80],
            target=target[:200],
            preview=preview[:2000],
            exec=exec_,
            created_by_run=run_id,
            signal_id=signal_exists(conn, p.get("signal_id")),
        )
        proposed.append(approvals.propose(conn, approval))
    return proposed


# --- Tier-0 fallback -------------------------------------------------------------------------------------


def fallback(ctx: Ctx, facts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Brief + risks from lookahead facts only (top 5). No model, no proposals."""
    facts = facts or lookahead.compute(ctx["conn"], ctx["tenant_id"], ctx["now"])
    top = lookahead.headlines(facts, limit=5)
    src = facts.get("sources_disconnected") or []
    if top:
        lines = [f"{i}. {h['title']} — {h['why']}" for i, h in enumerate(top, 1)]
        brief = "Chief of Staff (Tier 0, lookahead facts only):\n" + "\n".join(lines)
    else:
        brief = "Chief of Staff (Tier 0): nothing dated is due in the next 14 days and no stale approvals."
    if src:
        brief += "\nNo data: " + ", ".join(s["source"] for s in src) + "."
    return {
        "brief": brief,
        "risks": [{**h, "proposal_id": None} for h in top],
        "asks": [],
        "proposals": [],
        "changes_since_last": [],
        "degraded": True,
    }


# --- run ---------------------------------------------------------------------------------------------


def _call(ctx: Ctx, system: str, messages: list[dict[str, Any]], trigger: str) -> Any:
    return llm.call(
        2,
        NAME,
        system,
        messages,
        trigger=trigger,
        max_tokens=1800,
        high_priority=True,  # the hub agent still runs in conserve mode; exhausted → Tier-0 fallback
        conn=ctx["conn"],
        tenant_id=ctx["tenant_id"],
        temperature=0.2,
    )


def run(ctx: Ctx) -> dict[str, Any]:
    conn, tenant = ctx["conn"], ctx["tenant_id"]
    trigger = ctx.get("trigger") or "schedule"
    inputs = assemble_inputs(ctx)
    system = system_prompt(ctx, prompt_text())
    user = f"{USER_PROMPT}\n\n{json.dumps(inputs, ensure_ascii=False, sort_keys=True, default=str)}"
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]

    out: dict[str, Any] | None = None
    run_id: str | None = None
    status = "ok"
    try:
        res = _call(ctx, system, messages, trigger)
        run_id = res.run_id
        try:
            out = parse_output(res.text)
        except BadOutput as first:
            log.warning("chief_of_staff: invalid JSON (%s); retrying once", first)
            messages = messages + [
                {"role": "assistant", "content": res.text or "(empty)"},
                {"role": "user", "content": RETRY_NUDGE},
            ]
            res = _call(ctx, system, messages, trigger)
            run_id = res.run_id
            try:
                out = parse_output(res.text)
            except BadOutput as second:
                log.warning("chief_of_staff: still invalid after retry (%s); Tier-0 fallback", second)
                conn.execute("UPDATE runs SET status = 'degraded' WHERE id = %s", (run_id,))
                out = None
    except (llm.BudgetExhausted, llm.LLMUnavailable) as exc:
        log.info("chief_of_staff: %s; Tier-0 fallback", type(exc).__name__)
    except Exception as exc:  # API/network error is already in the ledger (status=error); keep the Cockpit alive
        log.exception("chief_of_staff: model call failed: %s", type(exc).__name__)

    if out is None:
        out = fallback(ctx, inputs["lookahead"])
        status = "degraded"
        run_id = llm.record_tier0(conn, tenant, NAME, trigger, out["brief"], status="degraded")
    else:
        conn.execute("UPDATE runs SET outcome = %s WHERE id = %s", (out["brief"][:20_000], run_id))

    sig = write_signals(ctx, out)
    proposals = write_proposals(ctx, out, run_id) if status == "ok" else []
    out["signals"] = sig
    out["approvals"] = [a.id for a in proposals]
    out["run_id"] = run_id
    out["status"] = status
    return out


SKILL = register(
    Skill(
        name=NAME,
        module="cockpit",
        tier=2,
        trigger="schedule",  # also invoked with trigger='ask' (/ask?mode=cos, Slack "what should I worry about")
        run=run,
        high_priority=True,
        description="Hub agent: cross-spoke brief, risks before they land, asks, proposals",
    )
)
