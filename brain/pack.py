"""Context pack compiler — deterministic Markdown ≤ 10,000 tokens (estimate = chars / 4).

    python -m brain.pack [--summarize] [--print]

Sections, in this order: Identity · ICP · Voice · Pricing · Team · Customers · Finance state · Build state ·
GTM state · Open signals (top 20 by severity) · Yesterday (counts). When over budget, the lowest-priority
sections are truncated first. Stored in `context_packs` with cache_key = sha256(content); an identical
pack is not stored twice.

`--summarize` is Tier 1 and goes through daemon/llm.py (imported lazily, only when the flag is used).
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from brain.sync import get_docs
from common.ids import content_key

MAX_TOKENS = 10_000
CHARS_PER_TOKEN = 4
TOP_SIGNALS = 20
TRUNC_MARK = "\n_(truncated to fit the pack budget)_\n"

SECTION_ORDER = [
    "identity",
    "icp",
    "voice",
    "pricing",
    "team",
    "customers",
    "finance",
    "build",
    "gtm",
    "signals",
    "yesterday",
]
TITLES = {
    "identity": "Identity",
    "icp": "ICP",
    "voice": "Voice",
    "pricing": "Pricing",
    "team": "Team",
    "customers": "Customers",
    "finance": "Finance state",
    "build": "Build state",
    "gtm": "GTM state",
    "signals": "Open signals",
    "yesterday": "Yesterday",
}
# Highest priority first; truncation starts from the end of this list.
PRIORITY = [
    "identity",
    "voice",
    "icp",
    "pricing",
    "team",
    "signals",
    "customers",
    "finance",
    "build",
    "gtm",
    "yesterday",
]
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _money(v: Any) -> str:
    d = v if isinstance(v, Decimal) else Decimal(str(v or 0))
    return f"{d:,.2f}"


def _demote(body: str) -> str:
    """Drop the doc's own H1 (the pack section already carries a title) and push other headings one level down."""
    lines = body.strip().splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    out = []
    for line in lines:
        out.append("#" + line if re.match(r"^#{2,5} ", line) else line)
    return "\n".join(out).strip()


def _doc_body(docs: dict[str, dict[str, Any]], slice_: str) -> str:
    doc = docs.get(slice_)
    return _demote(doc["body"]) if doc else "_(no brain doc for this slice yet)_"


# --- state sections ------------------------------------------------------------------


def finance_state(conn: psycopg.Connection, tenant_id: str, now: datetime, docs: dict) -> str:
    accts = conn.execute(
        "SELECT nickname, name, priority, available, inflow_mtd, outflow_mtd FROM accounts_bank WHERE tenant_id = %s ORDER BY priority, id",
        (tenant_id,),
    ).fetchall()
    bills = conn.execute(
        """SELECT vendor_name, amount, currency, due_at, payment_status FROM bills
           WHERE tenant_id = %s AND coalesce(payment_status,'') <> 'CLEARED' AND coalesce(status,'') NOT IN ('SETTLED','CANCELED','VOID')
           ORDER BY due_at NULLS LAST, id""",
        (tenant_id,),
    ).fetchall()
    lines = [_doc_body(docs, "finance"), "", "**Live tiles**"]
    if accts:
        for a in accts:
            label = a["nickname"] or a["name"] or "account"
            lines.append(
                f"- {label} ({a['priority'] or 'account'}): available {_money(a['available'])} · MTD in {_money(a['inflow_mtd'])} · MTD out {_money(a['outflow_mtd'])}"
            )
    else:
        lines.append("- no bank accounts ingested")
    due7 = [b for b in bills if b["due_at"] and b["due_at"] <= now + timedelta(days=7)]
    total_open = sum((b["amount"] for b in bills), Decimal("0"))
    total_due7 = sum((b["amount"] for b in due7), Decimal("0"))
    lines.append(
        f"- open bills: {len(bills)} totalling {_money(total_open)} · due within 7d: {len(due7)} ({_money(total_due7)})"
    )
    for b in due7:
        lines.append(
            f"  - {b['vendor_name']} · {_money(b['amount'])} {b['currency']} · due {b['due_at'].date().isoformat()} · {b['payment_status'] or ''}"
        )
    return "\n".join(lines)


def build_state(conn: psycopg.Connection, tenant_id: str, now: datetime, docs: dict) -> str:
    stats = conn.execute(
        """SELECT count(*) FILTER (WHERE coalesce(status_type,'') NOT IN ('completed','canceled')) AS open_issues,
                  count(*) FILTER (WHERE status_type = 'started') AS in_progress,
                  count(*) FILTER (WHERE priority IN (1,2) AND assignee IS NULL AND coalesce(status_type,'') NOT IN ('completed','canceled')) AS unassigned_high
           FROM issues WHERE tenant_id = %s""",
        (tenant_id,),
    ).fetchone()
    in_progress = conn.execute(
        "SELECT id, title, assignee, updated_at FROM issues WHERE tenant_id = %s AND status_type = 'started' ORDER BY updated_at, id",
        (tenant_id,),
    ).fetchall()
    projects = conn.execute(
        "SELECT name, status, target_date FROM projects WHERE tenant_id = %s ORDER BY name", (tenant_id,)
    ).fetchall()
    deploy = conn.execute(
        "SELECT id, project, state, created_at FROM deployments WHERE tenant_id = %s ORDER BY created_at DESC NULLS LAST, id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    lines = [_doc_body(docs, "build"), "", "**Live tiles**"]
    lines.append(
        f"- open issues: {stats['open_issues']} · in progress: {stats['in_progress']} · unassigned High/Urgent: {stats['unassigned_high']}"
    )
    for i in in_progress:
        age = (now - i["updated_at"]).days if i["updated_at"] else "?"
        lines.append(f"  - {i['id']} · {i['title']} · {i['assignee'] or 'unassigned'} · {age}d since update")
    if projects:
        lines.append("- projects: " + " · ".join(f"{p['name']} ({p['status'] or '—'})" for p in projects))
    if deploy:
        when = deploy["created_at"].date().isoformat() if deploy["created_at"] else "?"
        lines.append(f"- last deploy: {deploy['project']} {deploy['state']} on {when} ({deploy['id']})")
    return "\n".join(lines)


def gtm_state(conn: psycopg.Connection, tenant_id: str, now: datetime, docs: dict) -> str:
    seqs = conn.execute(
        "SELECT name, status, contacts, delivered, opened, clicked, replied, bounced FROM sequences WHERE tenant_id = %s ORDER BY name",
        (tenant_id,),
    ).fetchall()
    lines = [_doc_body(docs, "gtm")]
    if seqs:
        lines += ["", "**Sequences (ingested)**"]
        for s in seqs:
            lines.append(
                f"- {s['name']} ({s['status'] or '—'}): {s['contacts']} contacts · {s['delivered']} delivered · {s['opened']} opened · {s['clicked']} clicked · {s['replied']} replied · {s['bounced']} bounced"
            )
    return "\n".join(lines)


def customers_state(conn: psycopg.Connection, tenant_id: str, now: datetime, docs: dict) -> str:
    accounts = conn.execute(
        "SELECT name, stage, champion, icp_score, last_touch_at FROM accounts WHERE tenant_id = %s ORDER BY name",
        (tenant_id,),
    ).fetchall()
    asks = conn.execute(
        """SELECT id, title, labels, assignee, status FROM issues
           WHERE tenant_id = %s AND coalesce(status_type,'') NOT IN ('completed','canceled')
             AND EXISTS (SELECT 1 FROM unnest(labels) l WHERE l ILIKE 'customer:%%' OR l ILIKE 'account:%%')
           ORDER BY id""",
        (tenant_id,),
    ).fetchall()
    lines = [_doc_body(docs, "customers")]
    if accounts:
        lines += ["", "**Accounts (ingested)**"]
        for a in accounts:
            lines.append(
                f"- {a['name']} · {a['stage'] or '—'} · champion {a['champion'] or '—'} · ICP {a['icp_score'] or '—'}"
            )
    if asks:
        lines += ["", "**Open customer asks (Linear)**"]
        for i in asks:
            lines.append(
                f"- {i['id']} · {i['title']} · {', '.join(i['labels'])} · {i['assignee'] or 'unassigned'} · {i['status']}"
            )
    return "\n".join(lines)


def signals_section(conn: psycopg.Connection, tenant_id: str, now: datetime) -> str:
    rows = conn.execute(
        "SELECT id, module, severity, title, meta, suggested_skill, first_seen_at FROM signals WHERE tenant_id = %s AND resolved_at IS NULL",
        (tenant_id,),
    ).fetchall()
    rows = sorted(rows, key=lambda r: (SEVERITY_ORDER.get(r["severity"], 9), r["first_seen_at"] or now, r["id"]))
    if not rows:
        return "_(no open signals)_"
    lines = [f"{len(rows)} open; top {min(len(rows), TOP_SIGNALS)} by severity:"]
    for r in rows[:TOP_SIGNALS]:
        skill = f" → `{r['suggested_skill']}`" if r["suggested_skill"] else ""
        meta = f" — {r['meta']}" if r["meta"] else ""
        lines.append(f"- [{r['severity']}] {r['module']} · {r['title']}{meta}{skill} (`{r['id']}`)")
    return "\n".join(lines)


def yesterday_section(conn: psycopg.Connection, tenant_id: str, now: datetime) -> str:
    since = now - timedelta(days=1)
    events = conn.execute(
        """SELECT entity, kind, count(*) AS n FROM events
           WHERE tenant_id = %s AND occurred_at >= %s AND occurred_at < %s GROUP BY entity, kind ORDER BY entity, kind""",
        (tenant_id, since, now),
    ).fetchall()
    resolved = conn.execute(
        "SELECT count(*) AS n FROM signals WHERE tenant_id = %s AND resolved_at >= %s AND resolved_at < %s",
        (tenant_id, since, now),
    ).fetchone()["n"]
    approvals = conn.execute(
        "SELECT status, count(*) AS n FROM approvals WHERE tenant_id = %s GROUP BY status ORDER BY status", (tenant_id,)
    ).fetchall()
    total = sum(e["n"] for e in events)
    lines = [f"- {total} changes ingested in the last 24h"]
    for e in events:
        lines.append(f"  - {e['entity']} {e['kind']}: {e['n']}")
    lines.append(f"- {resolved} signals resolved")
    if approvals:
        lines.append("- approvals: " + " · ".join(f"{a['status']} {a['n']}" for a in approvals))
    return "\n".join(lines)


# --- assembly --------------------------------------------------------------------------


def build_sections(conn: psycopg.Connection, tenant_id: str, now: datetime) -> dict[str, str]:
    docs = get_docs(conn, tenant_id)
    identity = _doc_body(docs, "identity")
    if "decisions" in docs:
        identity += "\n\n### Recent decisions\n" + _demote(docs["decisions"]["body"])
    return {
        "identity": identity,
        "icp": _doc_body(docs, "icp"),
        "voice": _doc_body(docs, "voice"),
        "pricing": _doc_body(docs, "pricing"),
        "team": _doc_body(docs, "team"),
        "customers": customers_state(conn, tenant_id, now, docs),
        "finance": finance_state(conn, tenant_id, now, docs),
        "build": build_state(conn, tenant_id, now, docs),
        "gtm": gtm_state(conn, tenant_id, now, docs),
        "signals": signals_section(conn, tenant_id, now),
        "yesterday": yesterday_section(conn, tenant_id, now),
    }


def assemble(sections: dict[str, str], tenant_id: str, now: datetime, max_tokens: int = MAX_TOKENS) -> str:
    """Join sections in SECTION_ORDER under a header; truncate lowest-priority sections until under budget."""
    header = f"# Context pack · {tenant_id} · {now.date().isoformat()}\n"
    texts = {name: f"\n## {TITLES[name]}\n\n{sections.get(name, '').strip()}\n" for name in SECTION_ORDER}
    budget = max_tokens * CHARS_PER_TOKEN - len(header)
    total = sum(len(t) for t in texts.values())
    for name in reversed(PRIORITY):
        if total <= budget:
            break
        text = texts[name]
        head = f"\n## {TITLES[name]}\n"
        floor = len(head) + len(TRUNC_MARK)
        target = max(floor, len(text) - (total - budget))
        if target >= len(text):
            continue
        texts[name] = text[: target - len(TRUNC_MARK)].rstrip() + TRUNC_MARK
        total = sum(len(t) for t in texts.values())
    return header + "".join(texts[name] for name in SECTION_ORDER)


def store(conn: psycopg.Connection, tenant_id: str, content: str, now: datetime) -> dict[str, Any]:
    key = content_key(content)
    tokens = estimate_tokens(content)
    latest = conn.execute(
        "SELECT id, cache_key FROM context_packs WHERE tenant_id = %s ORDER BY compiled_at DESC, id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    if latest and latest["cache_key"] == key:
        return {"id": latest["id"], "cache_key": key, "token_estimate": tokens, "stored": False}
    row = conn.execute(
        """INSERT INTO context_packs (tenant_id, content, token_estimate, cache_key, compiled_at)
           VALUES (%s, %s, %s, %s, %s) RETURNING id""",
        (tenant_id, content, tokens, key, now),
    ).fetchone()
    return {"id": row["id"], "cache_key": key, "token_estimate": tokens, "stored": True}


def compile(conn: psycopg.Connection, tenant_id: str, now: datetime | None = None, *, summarize: bool = False) -> str:  # noqa: A001
    """Build, (optionally) summarize, store, and return the pack content."""
    now = now or datetime.now(UTC)
    sections = build_sections(conn, tenant_id, now)
    if summarize:
        sections = _summarize(sections, tenant_id)
    content = assemble(sections, tenant_id, now)
    store(conn, tenant_id, content, now)
    return content


def latest(conn: psycopg.Connection, tenant_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, content, token_estimate, cache_key, compiled_at FROM context_packs WHERE tenant_id = %s ORDER BY compiled_at DESC, id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    return dict(row) if row else None


def _summarize(sections: dict[str, str], tenant_id: str) -> dict[str, str]:
    """Tier 1 condensation of the state sections. Only reachable via --summarize; never used by tests."""
    from daemon import llm  # lazy: the only model dependency, and only when explicitly requested

    out = dict(sections)
    for name in ("customers", "finance", "build", "gtm"):
        result = llm.call(
            1,
            "brain.pack_summarize",
            "Condense this StartupOS context section. Keep every number, id and name. Markdown, no preamble.",
            [{"role": "user", "content": sections[name]}],
            trigger="schedule",
            max_tokens=800,
        )
        out[name] = result.text
    return out


def main(argv: list[str] | None = None) -> int:
    from common.db import ensure_tenant, get_conn
    from common.settings import settings

    parser = argparse.ArgumentParser(prog="brain.pack")
    parser.add_argument("--summarize", action="store_true", help="Tier 1 condensation via daemon/llm.py")
    parser.add_argument("--print", action="store_true", dest="print_pack", help="print the pack content")
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    with get_conn(args.dsn) as conn:
        ensure_tenant(conn, settings.tenant_id)
        content = compile(conn, settings.tenant_id, summarize=args.summarize)
        info = latest(conn, settings.tenant_id)
    print(f"context pack: {estimate_tokens(content)} tokens · cache_key {info['cache_key'][:12] if info else '?'}…")
    if args.print_pack:
        print(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
