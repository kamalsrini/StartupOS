"""Lookahead facts — Tier 0. What is *going* to happen, with ids and dates, for the Chief of Staff.

Pure functions over dict rows (no I/O, no model, no wall clock: every function takes `now`), plus
`compute(conn, tenant_id, now)` which loads the rows from Postgres and returns `facts(ctx, now)`.

    facts(ctx, now) -> dict   # stable key order, JSON-serializable (Decimal → "12500.00", dates → ISO)

Facts:
    cash            bills due ≤14d vs primary available cash; the date cash goes negative if all are paid
    runway          available / avg monthly outflow (last 90d of negative transactions, else outflow_mtd)
    issues_due      open issues whose raw.dueDate is ≤7d away (or already past)
    issues_untouched urgent/high open issues with no update for ≥5d
    milestones      POC milestones from customers.md front matter ≤14d away + the open customer-labelled issues
    campaigns       gtm.md campaigns with age since `since`; sequences with observed_at age
    deploys         production deploy cadence: last 30d vs prior 30d
    approvals       pending approvals older than 48h
    budget          the current month's budgets row
    sources         connections whose status != connected

The ctx is the `signals.engine.build_ctx` shape plus: approvals (pending rows), budget (row|None),
customers_md (str). Missing keys are treated as "no data".
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from brain.sync import parse_front_matter
from signals import engine
from signals.rules import _aware, account_label, is_open, monthly_outflow

BILL_HORIZON = timedelta(days=14)
DUE_HORIZON = timedelta(days=7)
UNTOUCHED_DAYS = 5
MILESTONE_HORIZON = timedelta(days=14)
DEPLOY_WINDOW = timedelta(days=30)
STALE_APPROVAL = timedelta(hours=48)
SETTLED_PAYMENT = {"CLEARED"}
SETTLED_STATUS = {"SETTLED", "CANCELED", "VOID"}


# --- helpers -----------------------------------------------------------------------


def _money(v: Any) -> Decimal:
    if v is None:
        return Decimal("0")
    return (v if isinstance(v, Decimal) else Decimal(str(v))).quantize(Decimal("0.01"))


def _m(v: Decimal) -> str:
    return f"{v:.2f}"


def _d(dt: datetime | None) -> str | None:
    return dt.date().isoformat() if dt else None


def _days_until(dt: datetime, now: datetime) -> int:
    return (dt.date() - now.date()).days


def _primary_account(accounts_bank: list[dict[str, Any]]) -> dict[str, Any] | None:
    primary = [a for a in accounts_bank if (a.get("priority") or "").upper() == "PRIMARY"]
    if not primary:
        primary = list(accounts_bank)
    return sorted(primary, key=lambda a: str(a["id"]))[0] if primary else None


def _bill_settled(b: dict[str, Any]) -> bool:
    return (b.get("payment_status") or "").upper() in SETTLED_PAYMENT or (
        b.get("status") or ""
    ).upper() in SETTLED_STATUS


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def jsonable(obj: Any) -> Any:
    """Decimal → str, datetime/date → ISO, dict keys sorted; lists kept in order."""
    if isinstance(obj, Decimal):
        return _m(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, list | tuple):
        return [jsonable(v) for v in obj]
    return obj


# --- cash & runway -------------------------------------------------------------------


def bills_vs_cash(
    bills: list[dict[str, Any]], accounts_bank: list[dict[str, Any]], now: datetime, horizon: timedelta = BILL_HORIZON
) -> dict[str, Any]:
    """Unsettled bills due ≤ horizon (overdue included) against primary available cash.
    `cash_negative_on` is the due date of the first bill whose cumulative amount exceeds available cash."""
    acct = _primary_account(accounts_bank)
    available = _money(acct.get("available")) if acct else None
    due: list[dict[str, Any]] = []
    for b in bills:
        if _bill_settled(b):
            continue
        at = _aware(b.get("due_at"))
        if not at or at > now + horizon:
            continue
        due.append(
            {
                "id": b["id"],
                "vendor": b.get("vendor_name"),
                "amount": _money(b.get("amount")),
                "currency": b.get("currency") or "USD",
                "due": _d(at),
                "days": _days_until(at, now),
                "payment_status": b.get("payment_status"),
            }
        )
    due.sort(key=lambda r: (r["due"], r["id"]))
    total = sum((r["amount"] for r in due), Decimal("0"))
    negative_on: str | None = None
    negative_bill: str | None = None
    if available is not None:
        running = available
        for r in due:
            running -= r["amount"]
            if running < 0:
                negative_on, negative_bill = r["due"], r["id"]
                break
    return {
        "account_id": acct["id"] if acct else None,
        "available": available,
        "bills_due": due,
        "total_due": total,
        "after_paying_all": (available - total) if available is not None else None,
        "cash_negative_on": negative_on,
        "cash_negative_bill": negative_bill,
        "horizon_days": int(horizon.total_seconds() // 86400),
    }


def runway(accounts_bank: list[dict[str, Any]], transactions: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """available / average monthly outflow. None when there is no account or no outflow observed."""
    acct = _primary_account(accounts_bank)
    if acct is None:
        return {"account_id": None, "available": None, "monthly_outflow": None, "months": None, "basis": "no data"}
    available = _money(acct.get("available"))
    ctx = {"transactions": transactions, "accounts_bank": accounts_bank}
    since = now - timedelta(days=90)
    from_tx = any(_money(t.get("amount")) < 0 and (_aware(t.get("occurred_at")) or now) >= since for t in transactions)
    outflow = monthly_outflow(ctx, now)
    months = None if outflow <= 0 else float((available / outflow).quantize(Decimal("0.1")))
    return {
        "account_id": acct["id"],
        "available": available,
        "monthly_outflow": outflow if outflow > 0 else None,
        "months": months,
        "basis": "transactions_90d" if from_tx else ("outflow_mtd" if outflow > 0 else "no data"),
    }


# --- issues ------------------------------------------------------------------------


def _due_date(issue: dict[str, Any]) -> datetime | None:
    raw = issue.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    due = (raw or {}).get("dueDate") or issue.get("due_date")
    try:
        return _aware(due) if due else None
    except (TypeError, ValueError):
        return None


def issues_due(issues: list[dict[str, Any]], now: datetime, horizon: timedelta = DUE_HORIZON) -> list[dict[str, Any]]:
    out = []
    for i in issues:
        due = _due_date(i)
        if not due or not is_open(i) or due > now + horizon:
            continue
        out.append(
            {
                "id": i["id"],
                "title": i.get("title"),
                "due": _d(due),
                "days": _days_until(due, now),
                "assignee": i.get("assignee"),
                "status": i.get("status"),
                "priority": i.get("priority"),
                "labels": list(i.get("labels") or []),
            }
        )
    return sorted(out, key=lambda r: (r["due"], r["id"]))


def issues_untouched(issues: list[dict[str, Any]], now: datetime, days: int = UNTOUCHED_DAYS) -> list[dict[str, Any]]:
    out = []
    for i in issues:
        upd = _aware(i.get("updated_at"))
        if i.get("priority") not in (1, 2) or not is_open(i) or not upd:
            continue
        idle = (now.date() - upd.date()).days
        if idle >= days:
            out.append(
                {
                    "id": i["id"],
                    "title": i.get("title"),
                    "priority": "urgent" if i["priority"] == 1 else "high",
                    "idle_days": idle,
                    "assignee": i.get("assignee"),
                    "status": i.get("status"),
                }
            )
    return sorted(out, key=lambda r: (-r["idle_days"], r["id"]))


# --- POC milestones ------------------------------------------------------------------


def parse_milestones(customers_md: str) -> list[dict[str, Any]]:
    """`milestones:` from customers.md front matter, as inline JSON or a YAML-ish block list:

    milestones:
      - account: jci
        milestone: POC readout
        due: 2026-09-12
    """
    if not customers_md or not customers_md.startswith("---"):
        return []
    meta, _ = parse_front_matter(customers_md)
    raw = meta.get("milestones")
    items: list[dict[str, Any]] = []
    if isinstance(raw, list):
        items = [m for m in raw if isinstance(m, dict)]
    else:
        lines = customers_md.splitlines()
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), len(lines))
        start = next((i for i in range(1, end) if lines[i].strip().startswith("milestones:")), None)
        if start is not None:
            cur: dict[str, Any] | None = None
            for line in lines[start + 1 : end]:
                if not line.strip():
                    continue
                if not line.startswith((" ", "\t", "-")):
                    break  # next top-level key
                body = line.strip()
                if body.startswith("- "):
                    cur = {}
                    items.append(cur)
                    body = body[2:].strip()
                if cur is None:
                    continue
                key, sep, val = body.partition(":")
                if sep:
                    cur[key.strip()] = val.strip().strip("'\"")
    out = []
    for m in items:
        account = str(m.get("account") or "").strip()
        milestone = str(m.get("milestone") or m.get("name") or "").strip()
        due = m.get("due") or m.get("date")
        if not account or not due:
            continue
        try:
            due_dt = _aware(str(due)) if not isinstance(due, datetime | date) else _aware(due)
        except (TypeError, ValueError):
            continue
        out.append({"account": account, "milestone": milestone or "milestone", "due": _d(due_dt)})
    return sorted(out, key=lambda r: (r["due"], r["account"], r["milestone"]))


def _account_matches(label: str | None, account: str) -> bool:
    if not label:
        return False
    slug = _slug(account)
    lab = _slug(label.split(":", 1)[-1].split("/", 1)[-1])
    return bool(slug) and (slug == lab or slug in lab or lab in slug)


def poc_milestones(
    milestones: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    now: datetime,
    horizon: timedelta = MILESTONE_HORIZON,
) -> list[dict[str, Any]]:
    """Milestones ≤ horizon away (overdue included) with the open customer-labelled issues for that account."""
    out = []
    for m in milestones:
        due = _aware(m["due"])
        if due is None or due > now + horizon:
            continue
        open_issues = sorted(
            i["id"] for i in issues if is_open(i) and _account_matches(account_label(i.get("labels")), m["account"])
        )
        out.append(
            {
                "account": m["account"],
                "milestone": m["milestone"],
                "due": m["due"],
                "days": _days_until(due, now),
                "open_issues": open_issues,
            }
        )
    return sorted(out, key=lambda r: (r["due"], r["account"]))


# --- GTM ---------------------------------------------------------------------------


def campaign_ages(gtm: dict[str, Any], sequences: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    camps = []
    for c in (gtm or {}).get("campaigns") or []:
        if not isinstance(c, dict):
            continue
        since = _aware(c.get("since") or c.get("drafted")) if (c.get("since") or c.get("drafted")) else None
        cid = str(c.get("id") or _slug(str(c.get("name"))))
        camps.append(
            {
                "id": cid,
                "name": c.get("name") or cid,
                "status": (c.get("status") or "unknown").lower(),
                "since": _d(since),
                "age_days": _days_until(now, since) if since else None,
                "targets": c.get("targets"),
            }
        )
    seqs = []
    for s in sequences:
        obs = _aware(s.get("observed_at"))
        seqs.append(
            {
                "id": s["id"],
                "name": s.get("name") or s["id"],
                "status": s.get("status"),
                "contacts": s.get("contacts") or 0,
                "replied": s.get("replied") or 0,
                "observed_at": _d(obs),
                "observed_age_days": _days_until(now, obs) if obs else None,
            }
        )
    return {
        "campaigns": sorted(camps, key=lambda r: r["id"]),
        "sequences": sorted(seqs, key=lambda r: str(r["id"])),
    }


# --- deploys, approvals, budget, sources -------------------------------------------


def deploy_cadence(
    deployments: list[dict[str, Any]], now: datetime, window: timedelta = DEPLOY_WINDOW
) -> dict[str, Any]:
    last, prior, failed_last = 0, 0, 0
    latest: datetime | None = None
    for d in deployments:
        if (d.get("target") or "production").lower() != "production":
            continue
        at = _aware(d.get("created_at"))
        if not at:
            continue
        if at > now - window:
            last += 1
            if (d.get("state") or "").upper() == "ERROR":
                failed_last += 1
            if latest is None or at > latest:
                latest = at
        elif at > now - 2 * window:
            prior += 1
    if last == prior == 0:
        trend = "no data"
    elif last > prior:
        trend = "up"
    elif last < prior:
        trend = "down"
    else:
        trend = "flat"
    return {
        "last_30d": last,
        "prior_30d": prior,
        "failed_last_30d": failed_last,
        "trend": trend,
        "last_prod_deploy": _d(latest),
        "days_since_last_prod_deploy": _days_until(now, latest) if latest else None,
    }


def stale_approvals(
    approvals: list[dict[str, Any]], now: datetime, older_than: timedelta = STALE_APPROVAL
) -> list[dict[str, Any]]:
    out = []
    for a in approvals:
        if (a.get("status") or "pending") != "pending":
            continue
        created = _aware(a.get("created_at"))
        if not created or now - created < older_than:
            continue
        out.append(
            {
                "id": a["id"],
                "module": a.get("module"),
                "type": a.get("type"),
                "target": a.get("target"),
                "age_hours": int((now - created).total_seconds() // 3600),
            }
        )
    return sorted(out, key=lambda r: (-r["age_hours"], r["id"]))


def budget_state(row: dict[str, Any] | None) -> dict[str, Any]:
    if not row:
        return {"month": None, "state": "no data", "tier2_used": None, "tier2_allowed": None, "pct": None}
    used = int(row.get("tier2_tokens_used") or 0)
    allowed = int(row.get("tier2_tokens_allowed") or 0)
    month = row.get("month")
    return {
        "month": month.isoformat() if isinstance(month, date) else month,
        "state": row.get("state") or "normal",
        "tier2_used": used,
        "tier2_allowed": allowed,
        "pct": round(100.0 * used / allowed, 1) if allowed else None,
        "cost_usd": _money(row.get("cost_usd")),
    }


def disconnected_sources(connections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = [
        {"source": c["source"], "status": c.get("status"), "last_error": c.get("last_error")}
        for c in connections
        if (c.get("status") or "connected") != "connected"
    ]
    return sorted(out, key=lambda r: r["source"])


# --- assembly ------------------------------------------------------------------------


def facts(ctx: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Pure: ctx (engine.build_ctx shape + approvals/budget/customers_md) → JSON-serializable facts."""
    issues = ctx.get("issues") or []
    milestones = parse_milestones(ctx.get("customers_md") or "")
    out = {
        "now": now.isoformat(),
        "cash": bills_vs_cash(ctx.get("bills") or [], ctx.get("accounts_bank") or [], now),
        "runway": runway(ctx.get("accounts_bank") or [], ctx.get("transactions") or [], now),
        "issues_due": issues_due(issues, now),
        "issues_untouched": issues_untouched(issues, now),
        "milestones": poc_milestones(milestones, issues, now),
        "gtm": campaign_ages(ctx.get("gtm") or {}, ctx.get("sequences") or [], now),
        "deploys": deploy_cadence(ctx.get("deployments") or [], now),
        "approvals_stale": stale_approvals(ctx.get("approvals") or [], now),
        "budget": budget_state(ctx.get("budget")),
        "sources_disconnected": disconnected_sources(ctx.get("connections") or []),
    }
    return jsonable(out)


def load_ctx(conn: psycopg.Connection, tenant_id: str, now: datetime) -> dict[str, Any]:
    ctx = engine.load_ctx(conn, tenant_id, now)
    ctx["approvals"] = engine._rows(
        conn,
        "SELECT id, module, type, target, status, created_at FROM approvals WHERE tenant_id = %s AND status = 'pending' ORDER BY id",
        (tenant_id,),
    )
    ctx["budget"] = conn.execute(
        "SELECT * FROM budgets WHERE tenant_id = %s AND month = %s", (tenant_id, date(now.year, now.month, 1))
    ).fetchone()
    ctx["budget"] = dict(ctx["budget"]) if ctx["budget"] else None
    row = conn.execute(
        "SELECT content FROM brain_docs WHERE tenant_id = %s AND (path = 'customers.md' OR slice = 'customers') ORDER BY path LIMIT 1",
        (tenant_id,),
    ).fetchone()
    ctx["customers_md"] = row["content"] if row else ""
    ctx["connections"] = engine._rows(
        conn,
        "SELECT source, status, config, last_error FROM connections WHERE tenant_id = %s ORDER BY source",
        (tenant_id,),
    )
    return ctx


def compute(conn: psycopg.Connection, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Load rows from Postgres and return the facts. Tier 0: no model, nothing written."""
    now = now or datetime.now(UTC)
    return facts(load_ctx(conn, tenant_id, now), now)


# --- Tier-0 rendering (used by the Chief of Staff fallback and the API) --------------------


def headlines(f: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    """The top facts as {module, severity, title, why, horizon_days}, ordered by irreversibility × time-to-impact."""
    items: list[tuple[int, dict[str, Any]]] = []
    cash = f.get("cash") or {}
    if cash.get("cash_negative_on"):
        items.append(
            (
                0,
                {
                    "module": "finance",
                    "severity": "high",
                    "horizon_days": 14,
                    "title": f"Cash goes negative on {cash['cash_negative_on']} if all bills are paid",
                    "why": f"available {cash.get('available')} vs {cash.get('total_due')} due ≤14d; first short bill {cash.get('cash_negative_bill')}",
                },
            )
        )
    rw = f.get("runway") or {}
    if rw.get("months") is not None and rw["months"] < 3:
        items.append(
            (
                1,
                {
                    "module": "finance",
                    "severity": "high",
                    "horizon_days": 30,
                    "title": f"Runway {rw['months']} months",
                    "why": f"available {rw.get('available')} / monthly outflow {rw.get('monthly_outflow')} ({rw.get('basis')}) · account {rw.get('account_id')}",
                },
            )
        )
    for b in cash.get("bills_due") or []:
        items.append(
            (
                2 if b["days"] <= 7 else 4,
                {
                    "module": "finance",
                    "severity": "high" if b["days"] <= 7 else "medium",
                    "horizon_days": max(b["days"], 0),
                    "title": f"{b.get('vendor') or 'Bill'} {b['amount']} {b['currency']} due {b['due']}",
                    "why": f"bill {b['id']} · {b.get('payment_status') or ''}".strip(),
                },
            )
        )
    for m in f.get("milestones") or []:
        items.append(
            (
                2,
                {
                    "module": "customers",
                    "severity": "high" if m["open_issues"] else "medium",
                    "horizon_days": max(m["days"], 0),
                    "title": f"{m['account']} · {m['milestone']} due {m['due']}",
                    "why": f"{len(m['open_issues'])} open customer issues: {', '.join(m['open_issues']) or 'none'}",
                },
            )
        )
    for i in f.get("issues_due") or []:
        items.append(
            (
                3,
                {
                    "module": "build",
                    "severity": "high" if i["days"] <= 2 else "medium",
                    "horizon_days": max(i["days"], 0),
                    "title": f"{i['id']} due {i['due']} · {i.get('title')}",
                    "why": f"{i.get('assignee') or 'unassigned'} · {i.get('status')}",
                },
            )
        )
    for i in f.get("issues_untouched") or []:
        items.append(
            (
                5,
                {
                    "module": "build",
                    "severity": "medium",
                    "horizon_days": 7,
                    "title": f"{i['id']} {i['priority']} untouched {i['idle_days']}d",
                    "why": f"{i.get('assignee') or 'unassigned'} · {i.get('status')}",
                },
            )
        )
    for a in f.get("approvals_stale") or []:
        items.append(
            (
                6,
                {
                    "module": a.get("module") or "cockpit",
                    "severity": "medium",
                    "horizon_days": 2,
                    "title": f"Approval {a['id']} waiting {a['age_hours']}h",
                    "why": f"{a.get('type')} · {a.get('target')}",
                },
            )
        )
    dp = f.get("deploys") or {}
    if dp.get("trend") == "down":
        items.append(
            (
                7,
                {
                    "module": "build",
                    "severity": "low",
                    "horizon_days": 30,
                    "title": f"Prod deploys down: {dp['last_30d']} last 30d vs {dp['prior_30d']} prior",
                    "why": f"last prod deploy {dp.get('last_prod_deploy')}",
                },
            )
        )
    for s in f.get("sources_disconnected") or []:
        items.append(
            (
                8,
                {
                    "module": "security",
                    "severity": "medium",
                    "horizon_days": 0,
                    "title": f"{s['source']} disconnected ({s.get('status')})",
                    "why": s.get("last_error") or "no data from this spoke",
                },
            )
        )
    bg = f.get("budget") or {}
    if bg.get("state") in ("conserve", "exhausted"):
        items.append(
            (
                8,
                {
                    "module": "cockpit",
                    "severity": "medium",
                    "horizon_days": 0,
                    "title": f"Model budget {bg['state']} ({bg.get('pct')}% used)",
                    "why": f"month {bg.get('month')}",
                },
            )
        )
    items.sort(key=lambda t: (t[0], t[1]["horizon_days"], t[1]["title"]))
    return [it for _, it in items[:limit]]
