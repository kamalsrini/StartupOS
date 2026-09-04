"""Cockpit: pulse (latest run outcome or Tier-0 fallback), tiles, quick glance, spend ledger, and the /ask retrieval preview."""

from __future__ import annotations

import html
import re
from datetime import date, timedelta
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import compact_money, get_db, get_tenant, iso, money, now_utc, scalar
from api.routers.finance import finance_summary_data
from api.routers.modules import MODULES, build_snapshot
from common.models import Tile

router = APIRouter(tags=["cockpit"])

PULSE_SKILL = "cockpit.morning_pulse"
# ts_headline does not escape source text; we mark hits with private sentinels, escape, then swap in <b>.
HEADLINE_OPTS = "MaxWords=40, MinWords=15, StartSel=@@HL@@, StopSel=@@/HL@@"


def _safe_snippet(text: str | None) -> str:
    return html.escape(text or "").replace("@@HL@@", "<b>").replace("@@/HL@@", "</b>")


GLANCE_MODULES = ("build", "customers", "sales", "marketing", "web", "security")


def _month_bounds(month: str | None) -> tuple[date, date]:
    if month:
        if not re.match(r"^\d{4}-\d{2}$", month):
            raise HTTPException(status_code=422, detail="month must be YYYY-MM")
        y, m = (int(x) for x in month.split("-"))
        start = date(y, m, 1)
    else:
        start = date.today().replace(day=1)
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


def runs_summary_data(conn: psycopg.Connection, tenant_id: str, month: str | None) -> dict[str, Any]:
    start, end = _month_bounds(month)
    by_tier = conn.execute(
        """SELECT tier, count(*) AS runs, sum(tokens_in) AS tokens_in, sum(tokens_cached) AS tokens_cached,
                  sum(tokens_out) AS tokens_out, sum(cost_usd) AS cost_usd
             FROM runs WHERE tenant_id=%s AND started_at >= %s AND started_at < %s GROUP BY tier ORDER BY tier""",
        (tenant_id, start, end),
    ).fetchall()
    by_skill = conn.execute(
        """SELECT skill, tier, count(*) AS runs, sum(tokens_in) AS tokens_in, sum(tokens_cached) AS tokens_cached,
                  sum(tokens_out) AS tokens_out, sum(cost_usd) AS cost_usd
             FROM runs WHERE tenant_id=%s AND started_at >= %s AND started_at < %s GROUP BY skill, tier ORDER BY cost_usd DESC, skill""",
        (tenant_id, start, end),
    ).fetchall()
    budget = conn.execute("SELECT * FROM budgets WHERE tenant_id=%s AND month=%s", (tenant_id, start)).fetchone()

    def fmt(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            d = dict(r)
            d["cost_usd"] = f"{(d['cost_usd'] or 0):.5f}"
            for k in ("tokens_in", "tokens_cached", "tokens_out"):
                d[k] = int(d[k] or 0)
            out.append(d)
        return out

    total_cost = sum(float(r["cost_usd"] or 0) for r in by_tier)
    total_tokens = sum(int((r["tokens_in"] or 0) + (r["tokens_out"] or 0)) for r in by_tier)
    return {
        "month": start.strftime("%Y-%m"),
        "runs": sum(int(r["runs"]) for r in by_tier),
        "tokens": total_tokens,
        "cost_usd": f"{total_cost:.5f}",
        "by_tier": fmt(by_tier),
        "by_skill": fmt(by_skill),
        "budget": {
            "tier2_tokens_allowed": budget["tier2_tokens_allowed"],
            "tier2_tokens_used": budget["tier2_tokens_used"],
            "tier1_tokens_used": budget["tier1_tokens_used"],
            "state": budget["state"],
            "cost_usd": money(budget["cost_usd"]),
        }
        if budget
        else None,
    }


@router.get("/runs/summary")
def runs_summary(
    month: str | None = Query(default=None, description="YYYY-MM; default current month"),
    conn: psycopg.Connection = Depends(get_db),
    tenant_id: str = Depends(get_tenant),
) -> dict[str, Any]:
    return runs_summary_data(conn, tenant_id, month)


def tier0_pulse(conn: psycopg.Connection, tenant_id: str) -> str:
    """Template text from open high signals and pending approvals. No model."""
    highs = conn.execute(
        "SELECT module, title FROM signals WHERE tenant_id=%s AND resolved_at IS NULL AND severity='high' ORDER BY last_seen_at DESC LIMIT 3",
        (tenant_id,),
    ).fetchall()
    n_open = scalar(conn, "SELECT count(*) FROM signals WHERE tenant_id=%s AND resolved_at IS NULL", (tenant_id,))
    n_pending = scalar(conn, "SELECT count(*) FROM approvals WHERE tenant_id=%s AND status='pending'", (tenant_id,))
    today = now_utc().strftime("%a %b %-d")
    if not highs and not n_open and not n_pending:
        return f"{today}. No open signals and nothing waiting for approval. Connect a source or run ingest to populate the cockpit."
    lines = [
        f"{today}. {n_open} open signal{'s' if n_open != 1 else ''}, {n_pending} approval{'s' if n_pending != 1 else ''} waiting."
    ]
    for h in highs:
        lines.append(f"• [{h['module']}] {h['title']}")
    return "\n".join(lines)


@router.get("/cockpit")
def cockpit(conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)) -> dict[str, Any]:
    latest = conn.execute(
        "SELECT outcome, started_at, model, tier FROM runs WHERE tenant_id=%s AND skill=%s AND outcome IS NOT NULL ORDER BY started_at DESC LIMIT 1",
        (tenant_id, PULSE_SKILL),
    ).fetchone()
    if latest:
        pulse = {
            "text": latest["outcome"],
            "at": iso(latest["started_at"]),
            "tier": latest["tier"],
            "model": latest["model"],
            "source": "run",
        }
    else:
        pulse = {
            "text": tier0_pulse(conn, tenant_id),
            "at": iso(now_utc()),
            "tier": 0,
            "model": None,
            "source": "fallback",
        }

    fin = finance_summary_data(conn, tenant_id)
    since = now_utc() - timedelta(days=90)
    cash_in = conn.execute(
        "SELECT coalesce(sum(amount),0) AS v, count(*) AS n FROM transactions WHERE tenant_id=%s AND amount>0 AND occurred_at>=%s",
        (tenant_id, since),
    ).fetchone()
    stages = conn.execute(
        "SELECT stage, count(*) AS n FROM accounts WHERE tenant_id=%s GROUP BY stage", (tenant_id,)
    ).fetchall()
    stage_map = {s["stage"]: s["n"] for s in stages}
    pipeline_n = sum(v for k, v in stage_map.items() if k in ("engaged", "poc"))
    pending = scalar(conn, "SELECT count(*) FROM approvals WHERE tenant_id=%s AND status='pending'", (tenant_id,))
    open_high = scalar(
        conn,
        "SELECT count(*) FROM signals WHERE tenant_id=%s AND resolved_at IS NULL AND severity='high'",
        (tenant_id,),
    )
    n_conn = scalar(conn, "SELECT count(*) FROM connections WHERE tenant_id=%s AND status='connected'", (tenant_id,))

    tiles = [
        Tile(
            label="Health",
            val="—" if not n_conn else ("Attention" if open_high else "OK"),
            sub=f"{n_conn} sources · {open_high} high signals" if n_conn else "no sources connected",
            cls="warn" if open_high else "",
        ),
        Tile(
            label="Cash in (90d)",
            val=compact_money(cash_in["v"]),
            sub=f"{cash_in['n']} inflows · cash on hand ${fin['cash_on_hand']}",
        ),
        Tile(
            label="Pipeline",
            val=str(pipeline_n),
            sub=" · ".join(f"{k} {v}" for k, v in sorted(stage_map.items()) if k) or "no accounts ingested",
        ),
        Tile(label="Pending approvals", val=str(pending), sub="waiting for you", cls="warn" if pending else ""),
    ]

    glance = []
    for name in GLANCE_MODULES:
        snap = build_snapshot(conn, tenant_id, name)
        t0 = snap.tiles[0]
        glance.append(
            {
                "module": name,
                "title": MODULES[name]["title"],
                "line": f"{t0.label} {t0.val} · {len(snap.signals)} signals · {len(snap.approvals)} approvals",
                "signals": len(snap.signals),
                "approvals": len(snap.approvals),
                "cls": "warn" if any(t.cls for t in snap.tiles) else "",
            }
        )

    return {
        "tenant_id": tenant_id,
        "snapshot_at": iso(now_utc()),
        "pulse": pulse,
        "tiles": [t.model_dump() for t in tiles],
        "quick_glance": glance,
        "pending_approvals": pending,
        "finance": fin,
        "spend": runs_summary_data(conn, tenant_id, None),
    }


@router.get("/ask")
def ask(
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=8, ge=1, le=50),
    conn: psycopg.Connection = Depends(get_db),
    tenant_id: str = Depends(get_tenant),
) -> dict[str, Any]:
    """Retrieval preview over brain_docs / messages / documents via Postgres FTS. The daemon answers with a model; this does not."""
    hits: list[dict[str, Any]] = []
    rows = conn.execute(
        """SELECT path, slice, version, ts_rank(tsv, q) AS rank, ts_headline('english', content, q, %s) AS snippet
             FROM brain_docs, websearch_to_tsquery('english', %s) q
            WHERE tenant_id=%s AND tsv @@ q ORDER BY rank DESC LIMIT %s""",
        (HEADLINE_OPTS, q, tenant_id, limit),
    ).fetchall()
    hits += [
        {
            "source": "brain_docs",
            "id": r["path"],
            "title": f"{r['slice']} · v{r['version']}",
            "snippet": r["snippet"],
            "rank": float(r["rank"]),
            "url": None,
        }
        for r in rows
    ]
    rows = conn.execute(
        """SELECT id, source, channel, author, occurred_at, ts_rank(tsv, q) AS rank, ts_headline('english', text, q, %s) AS snippet
             FROM messages, websearch_to_tsquery('english', %s) q
            WHERE tenant_id=%s AND tsv @@ q ORDER BY rank DESC, occurred_at DESC LIMIT %s""",
        (HEADLINE_OPTS, q, tenant_id, limit),
    ).fetchall()
    hits += [
        {
            "source": f"messages:{r['source']}",
            "id": r["id"],
            "title": f"#{r['channel'] or '?'} · {r['author'] or '?'} · {r['occurred_at']:%b %-d}",
            "snippet": r["snippet"],
            "rank": float(r["rank"]),
            "url": None,
        }
        for r in rows
    ]
    rows = conn.execute(
        """SELECT id, source, title, url, ts_rank(tsv, q) AS rank, ts_headline('english', content, q, %s) AS snippet
             FROM documents, websearch_to_tsquery('english', %s) q
            WHERE tenant_id=%s AND tsv @@ q ORDER BY rank DESC LIMIT %s""",
        (HEADLINE_OPTS, q, tenant_id, limit),
    ).fetchall()
    hits += [
        {
            "source": f"documents:{r['source']}",
            "id": r["id"],
            "title": r["title"] or "(untitled)",
            "snippet": r["snippet"],
            "rank": float(r["rank"]),
            "url": r["url"],
        }
        for r in rows
    ]
    for h in hits:
        h["snippet"] = _safe_snippet(h["snippet"])
    hits.sort(key=lambda h: h["rank"], reverse=True)
    return {
        "q": q,
        "hits": hits[:limit],
        "total": len(hits),
        "answered_by": None,
        "note": "Retrieval preview only — the daemon's ask.answer skill composes the answer.",
    }
