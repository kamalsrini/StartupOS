"""ask.answer — "Ask the OS" (Tier 2). Retrieval via Postgres FTS over brain_docs / messages / documents, then
one model call with the context pack + retrieved passages; the answer cites its sources.

Without a model (no key, budget exhausted) it returns the retrieved sources so the founder still gets something.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg

from daemon import llm
from daemon.skills.base import Ctx, Skill, register, system_prompt

NAME = "ask.answer"
TOP_K = 5
SNIPPET = 700

INSTRUCTIONS = """
You answer the founder's question using ONLY the context pack above and the numbered sources in the user message.
Be direct and specific (names, ids, amounts, dates). After the answer add a line "Sources:" and list the source
numbers you relied on as [n]. If the sources do not contain the answer, say so and suggest where to look.
"""


def retrieve(conn: psycopg.Connection, tenant_id: str, question: str, k: int = TOP_K) -> list[dict[str, Any]]:
    """Top-k passages from brain_docs, messages, documents by ts_rank over the generated tsv columns.

    Two passes: strict (all terms, websearch syntax) then loose (any term) when the strict pass finds nothing.
    """
    q = (question or "").strip()
    if not q:
        return []
    strict = "websearch_to_tsquery('english', %s)"
    loose = "to_tsquery('english', %s)"
    terms = " | ".join(dict.fromkeys(t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}", q)))
    hits = _search(conn, tenant_id, strict, q, k)
    if not hits and terms:
        hits = _search(conn, tenant_id, loose, terms, k)
    hits.sort(key=lambda h: float(h.get("rank") or 0), reverse=True)
    return hits[: k * 2]


def _search(conn: psycopg.Connection, tenant_id: str, tsq: str, q: str, k: int) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    sql = [
        f"""SELECT 'brain:' || path AS ref, path AS title, content, updated_at AS at, ts_rank(tsv, {tsq}) AS rank
            FROM brain_docs WHERE tenant_id = %s AND tsv @@ {tsq} ORDER BY rank DESC LIMIT %s""",
        f"""SELECT 'slack:' || coalesce(channel,'') || '/' || id AS ref,
                   coalesce(author,'') || ' in ' || coalesce(channel,'') AS title, text AS content, occurred_at AS at,
                   ts_rank(tsv, {tsq}) AS rank
            FROM messages WHERE tenant_id = %s AND tsv @@ {tsq} ORDER BY rank DESC, occurred_at DESC LIMIT %s""",
        f"""SELECT 'doc:' || source || '/' || id AS ref, coalesce(title, id) AS title, content, updated_at AS at,
                   ts_rank(tsv, {tsq}) AS rank
            FROM documents WHERE tenant_id = %s AND tsv @@ {tsq} ORDER BY rank DESC LIMIT %s""",
    ]
    for stmt in sql:
        for r in conn.execute(stmt, (q, tenant_id, q, k)).fetchall():
            d = dict(r)
            d["content"] = (d["content"] or "")[:SNIPPET]
            hits.append(d)
    return hits


def _sources_block(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "(no matching sources in the brain)"
    lines = []
    for i, h in enumerate(hits, 1):
        when = h["at"].date().isoformat() if h.get("at") else ""
        lines.append(f"[{i}] {h['ref']} — {h['title']} {when}\n{h['content']}")
    return "\n\n".join(lines)


def _sources_footer(hits: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{i}] {h['ref']}" for i, h in enumerate(hits, 1))


def answer(ctx: Ctx, question: str) -> str:
    conn, tenant = ctx["conn"], ctx["tenant_id"]
    hits = retrieve(conn, tenant, question)
    user = f"Question: {question}\n\nSources:\n{_sources_block(hits)}"
    try:
        res = llm.call(
            2,
            NAME,
            system_prompt(ctx, INSTRUCTIONS),
            [{"role": "user", "content": user}],
            trigger="ask",
            max_tokens=700,
            high_priority=True,
            conn=conn,
            tenant_id=tenant,
        )
    except (llm.BudgetExhausted, llm.LLMUnavailable):
        text = (
            "I can't draft an answer right now (model budget/credentials unavailable). "
            "Here is what the brain has on that:\n" + _sources_footer(hits)
            if hits
            else "I can't draft an answer right now, and nothing in the brain matched that question."
        )
        llm.record_tier0(conn, tenant, NAME, "ask", text, status="degraded")
        return text
    text = res.text
    if hits and "Sources:" not in text:
        text += "\n\nSources:\n" + _sources_footer(hits)
    return text


def run(ctx: Ctx) -> str:
    return answer(ctx, ctx.get("question") or "")


SKILL = register(
    Skill(
        name=NAME,
        module="ask",
        tier=2,
        trigger="ask",
        run=run,
        high_priority=True,
        description="Ask the OS: FTS retrieval + answer with sources",
    )
)
