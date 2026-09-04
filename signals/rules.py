"""Rule registry — the v1 set from CONTRACTS.md. Pure functions over dict rows; no I/O, no model.

Every rule is `evaluate(ctx, now) -> list[signal dict]`. `ctx` is a plain dict built by `signals.engine`:

    issues            list of issues rows (labels: list[str], updated_at: aware datetime, priority: int|None)
    bills             list of bills rows (amount: Decimal, due_at: aware datetime|None)
    accounts_bank     list of accounts_bank rows (available/outflow_mtd: Decimal, priority: PRIMARY|NON_PRIMARY)
    transactions      list of transactions rows (amount signed Decimal, occurred_at: aware datetime)
    deployments       list of deployments rows (state, created_at)
    sequences         list of sequences rows
    sequence_events   list of events rows for entity='sequences' in the observation window (diff: dict)
    connections       list of connections rows (source, status, config)
    accounts          list of accounts rows (name) — known customers, used to match inflows
    invoices          list of {counterparty, amount} — v1 has no invoice table, so usually []
    gtm               front matter dict of brain gtm.md (campaigns: [{id, name, status, since}], ...)
    social            {"linkedin_pending": int}
    config            {"cash_floor": Decimal|None}

Signal ids are `f"{rule_id}:{entity_id}"`, so re-evaluation is idempotent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

OPEN_STATUS_TYPES = {"backlog", "unstarted", "started", "triage", None}
DONE_STATUS_TYPES = {"completed", "canceled", "cancelled"}
DONE_STATUSES = {"done", "canceled", "cancelled", "closed", "duplicate"}
ACCOUNT_LABEL_PREFIXES = ("customer:", "account:", "customer/", "account/")

STALE_IN_PROGRESS = timedelta(days=7)
DEPLOY_WINDOW = timedelta(days=7)
BILL_DUE_WINDOW = timedelta(days=7)
ASK_UNTOUCHED_DAYS = 3  # calendar days since the last update
STALE_CAMPAIGN = timedelta(days=14)
CASH_RUNWAY_MONTHS = 3
INFLOW_WINDOW = timedelta(days=90)
LINKEDIN_BACKLOG = 25


@dataclass(frozen=True)
class Rule:
    id: str
    module: str
    severity: str
    kind: str
    suggested_skill: str | None
    evaluate: Callable[[dict[str, Any], datetime], list[dict[str, Any]]]

    def signal(
        self,
        entity: str,
        entity_id: str,
        title: str,
        meta: str = "",
        href: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": f"{self.id}:{entity_id}",
            "module": self.module,
            "rule_id": self.id,
            "severity": self.severity,
            "kind": self.kind,
            "title": title,
            "meta": meta,
            "entity": entity,
            "entity_id": entity_id,
            "suggested_skill": self.suggested_skill,
            "href": href,
        }


# --- helpers -----------------------------------------------------------------------


def _aware(dt: Any) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    if isinstance(dt, date):
        return datetime(dt.year, dt.month, dt.day, tzinfo=UTC)
    if isinstance(dt, str):
        s = dt[:-1] + "+00:00" if dt.endswith("Z") else dt
        parsed = datetime.fromisoformat(s)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise TypeError(f"not a datetime: {dt!r}")


def is_open(issue: dict[str, Any]) -> bool:
    st = (issue.get("status_type") or "").lower() or None
    if st in DONE_STATUS_TYPES:
        return False
    if st is None and (issue.get("status") or "").lower() in DONE_STATUSES:
        return False
    return True


def normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _money(v: Any) -> Decimal:
    if v is None:
        return Decimal("0")
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _fmt_money(v: Decimal, ccy: str = "USD") -> str:
    return f"{v:,.2f} {ccy}"


def _days(delta: timedelta) -> int:
    return int(delta.total_seconds() // 86400)


def account_label(labels: Iterable[str] | None) -> str | None:
    for label in labels or []:
        low = (label or "").lower()
        for prefix in ACCOUNT_LABEL_PREFIXES:
            if low.startswith(prefix):
                return label
    return None


# --- rules -------------------------------------------------------------------------


def _unassigned_high(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    out = []
    for i in sorted(ctx.get("issues", []), key=lambda r: r["id"]):
        if i.get("priority") in (1, 2) and not i.get("assignee") and is_open(i):
            prio = "Urgent" if i["priority"] == 1 else "High"
            out.append(
                R_UNASSIGNED_HIGH.signal(
                    "issues",
                    i["id"],
                    f"{i['id']} · {i['title']}",
                    f"{prio} · unassigned · {i.get('status') or 'open'}",
                    i.get("url"),
                )
            )
    return out


def _stale_in_progress(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    out = []
    for i in sorted(ctx.get("issues", []), key=lambda r: r["id"]):
        started = (i.get("status_type") or "").lower() == "started" or (i.get("status") or "").lower() == "in progress"
        upd = _aware(i.get("updated_at"))
        if started and upd and now - upd > STALE_IN_PROGRESS:
            out.append(
                R_STALE_IN_PROGRESS.signal(
                    "issues",
                    i["id"],
                    f"{i['id']} · {i['title']}",
                    f"In Progress · no update for {_days(now - upd)} days · {i.get('assignee') or 'unassigned'}",
                    i.get("url"),
                )
            )
    return out


def _duplicate_titles(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for i in ctx.get("issues", []):
        if is_open(i) and i.get("title"):
            groups.setdefault(normalize_title(i["title"]), []).append(i)
    out = []
    for _norm, issues in sorted(groups.items()):
        if len(issues) < 2:
            continue
        issues = sorted(issues, key=lambda r: r["id"])
        ids = [i["id"] for i in issues]
        entity_id = "+".join(ids)
        out.append(
            R_DUPLICATE_TITLES.signal(
                "issues",
                entity_id,
                f"Duplicate open issues: {issues[0]['title']}",
                f"{len(ids)} issues · {', '.join(ids)}",
                issues[0].get("url"),
            )
        )
    return out


def _deploy_failed(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    deployments = ctx.get("deployments", [])
    # PE review 2026-09-04: the window is anchored to `now`, never to the feed. A failure older than
    # DEPLOY_WINDOW is history, not a signal — fixtures must carry realistic dates instead.
    anchor = now
    out = []
    for d in sorted(deployments, key=lambda r: r["id"]):
        at = _aware(d.get("created_at"))
        if (d.get("state") or "").upper() == "ERROR" and at and anchor - at <= DEPLOY_WINDOW:
            out.append(
                R_DEPLOY_FAILED.signal(
                    "deployments",
                    d["id"],
                    f"Deploy {d['id']} failed · {d.get('project')}",
                    f"{d.get('target') or 'deploy'} · {at.date().isoformat()} · {d.get('commit_message') or ''}".strip(),
                    d.get("url"),
                )
            )
    return out


def _bill_due_7d(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    out = []
    horizon = now + BILL_DUE_WINDOW
    for b in sorted(ctx.get("bills", []), key=lambda r: r["id"]):
        if (b.get("payment_status") or "").upper() == "CLEARED" or (b.get("status") or "").upper() in {
            "SETTLED",
            "CANCELED",
            "VOID",
        }:
            continue
        due = _aware(b.get("due_at"))
        if due and due <= horizon:
            when = "overdue" if due < now else f"due in {_days(due - now)}d"
            out.append(
                R_BILL_DUE_7D.signal(
                    "bills",
                    b["id"],
                    f"{b.get('vendor_name') or 'Bill'} · {_fmt_money(_money(b['amount']), b.get('currency') or 'USD')}",
                    f"{when} ({due.date().isoformat()}) · {b.get('payment_status') or b.get('status') or ''}".strip(),
                    f"https://dashboard.brex.com/p/bills/{b['id']}",
                )
            )
    return out


def monthly_outflow(ctx: dict[str, Any], now: datetime) -> Decimal:
    """Average monthly outflow: outgoing transactions in the last 90 days / 3, else the primary account's MTD."""
    since = now - INFLOW_WINDOW
    total = Decimal("0")
    seen = False
    for t in ctx.get("transactions", []):
        at = _aware(t.get("occurred_at"))
        amt = _money(t.get("amount"))
        if amt < 0 and at and at >= since:
            total += -amt
            seen = True
    if seen:
        return (total / 3).quantize(Decimal("0.01"))
    for a in ctx.get("accounts_bank", []):
        if (a.get("priority") or "").upper() == "PRIMARY":
            return _money(a.get("outflow_mtd"))
    return Decimal("0")


def _cash_low(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    primary = [a for a in ctx.get("accounts_bank", []) if (a.get("priority") or "").upper() == "PRIMARY"]
    if not primary:
        return []
    acct = sorted(primary, key=lambda r: r["id"])[0]
    available = _money(acct.get("available"))
    burn = monthly_outflow(ctx, now)
    floor = ctx.get("config", {}).get("cash_floor")
    threshold = burn * CASH_RUNWAY_MONTHS
    reason = f"< {CASH_RUNWAY_MONTHS}× monthly outflow ({_fmt_money(burn)})"
    if floor is not None and _money(floor) > threshold:
        threshold = _money(floor)
        reason = f"< configured floor ({_fmt_money(_money(floor))})"
    if threshold <= 0 or available >= threshold:
        return []
    runway = f"{(available / burn):.1f} months runway" if burn > 0 else "no outflow observed"
    return [
        R_CASH_LOW.signal(
            "accounts_bank",
            acct["id"],
            f"Cash low · {acct.get('nickname') or acct.get('name') or acct['id']} at {_fmt_money(available)}",
            f"{reason} · {runway}",
            "https://dashboard.brex.com/accounts",
        )
    ]


def _norm_party(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _unmatched_inflow(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    known = {_norm_party(a.get("name")) for a in ctx.get("accounts", []) if a.get("name")}
    invoices = {_norm_party(i.get("counterparty")) for i in ctx.get("invoices", []) if i.get("counterparty")}
    since = now - INFLOW_WINDOW
    out = []
    for t in sorted(ctx.get("transactions", []), key=lambda r: r["id"]):
        amt = _money(t.get("amount"))
        at = _aware(t.get("occurred_at"))
        if amt <= 0 or not at or at < since:
            continue
        party = _norm_party(t.get("counterparty"))
        if party and (party in invoices or any(party in k or k in party for k in known if k)):
            continue
        out.append(
            R_UNMATCHED_INFLOW.signal(
                "transactions",
                t["id"],
                f"Inflow {_fmt_money(amt, t.get('currency') or 'USD')} from {t.get('counterparty') or 'unknown'}",
                f"{t.get('type') or 'transfer'} · {at.date().isoformat()} · no matching invoice or account",
                "https://dashboard.brex.com/accounts",
            )
        )
    return out


def _ask_untouched(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    out = []
    for i in sorted(ctx.get("issues", []), key=lambda r: r["id"]):
        label = account_label(i.get("labels"))
        upd = _aware(i.get("updated_at"))
        if not label or not upd or not is_open(i):
            continue
        idle_days = (now.date() - upd.date()).days
        if idle_days >= ASK_UNTOUCHED_DAYS:
            account = label.split(":", 1)[-1].split("/", 1)[-1]
            out.append(
                R_ASK_UNTOUCHED.signal(
                    "issues",
                    i["id"],
                    f"{i['id']} · {i['title']}",
                    f"{account} · untouched {idle_days} days · {i.get('assignee') or 'unassigned'} · {i.get('status') or ''}".strip(),
                    i.get("url"),
                )
            )
    return out


def _reply_detected(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    by_id = {s["id"]: s for s in ctx.get("sequences", [])}
    out = []
    seen: set[str] = set()
    for ev in sorted(ctx.get("sequence_events", []), key=lambda e: (str(e.get("entity_id")), str(e.get("id", "")))):
        diff = ev.get("diff") or {}
        change = diff.get("replied")
        if not change or len(change) != 2:
            continue
        old, new = change
        if new is None or (old is not None and int(new) <= int(old)):
            continue
        sid = str(ev["entity_id"])
        if sid in seen:
            continue
        seen.add(sid)
        seq = by_id.get(sid, {})
        delta = int(new) - int(old or 0)
        out.append(
            R_REPLY_DETECTED.signal(
                "sequences",
                sid,
                f"{delta} new repl{'y' if delta == 1 else 'ies'} · {seq.get('name') or sid}",
                f"replied {old or 0} → {new} · {seq.get('contacts') or 0} contacts",
                None,
            )
        )
    return out


def _stale_draft_campaign(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    out = []
    campaigns = (ctx.get("gtm") or {}).get("campaigns") or []
    for c in sorted(campaigns, key=lambda c: str(c.get("id") or c.get("name"))):
        if (c.get("status") or "").lower() != "draft":
            continue
        since = _aware(c.get("since"))
        if not since or now - since <= STALE_CAMPAIGN:
            continue
        cid = str(c.get("id") or re.sub(r"[^a-z0-9]+", "-", str(c.get("name")).lower()).strip("-"))
        targets = c.get("targets")
        out.append(
            R_STALE_DRAFT_CAMPAIGN.signal(
                "campaigns",
                cid,
                f"{c.get('name') or cid} in draft for {_days(now - since)} days",
                f"draft since {since.date().isoformat()}" + (f" · {targets} scored targets" if targets else ""),
                None,
            )
        )
    return out


def _analytics_off(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    conns = ctx.get("connections", [])
    active = {c["source"] for c in conns if (c.get("status") or "connected") != "disabled"}
    vercel_analytics = any(
        c["source"] == "vercel" and (c.get("config") or {}).get("analytics") for c in conns if c.get("config")
    )
    if "posthog" in active or vercel_analytics:
        return []
    return [
        R_ANALYTICS_OFF.signal(
            "connections",
            "posthog",
            "Site analytics not connected",
            "no PostHog connection and Vercel Web Analytics not enabled",
            None,
        )
    ]


def _queue_backlog(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    pending = (ctx.get("social") or {}).get("linkedin_pending") or 0
    if int(pending) <= LINKEDIN_BACKLOG:
        return []
    return [
        R_QUEUE_BACKLOG.signal(
            "social_queue",
            "linkedin",
            f"LinkedIn queue backlog · {pending} pending",
            f"> {LINKEDIN_BACKLOG} DMs waiting for review",
            None,
        )
    ]


R_UNASSIGNED_HIGH = Rule("build.unassigned_high", "build", "high", "click", "build.assign_owner", _unassigned_high)
R_STALE_IN_PROGRESS = Rule(
    "build.stale_in_progress", "build", "medium", "open", "build.nudge_stale", _stale_in_progress
)
R_DUPLICATE_TITLES = Rule(
    "build.duplicate_titles", "security", "medium", "reply", "security.merge_duplicates", _duplicate_titles
)
R_DEPLOY_FAILED = Rule("build.deploy_failed", "web", "medium", "bounce", "build.log_failed_deploy", _deploy_failed)
R_BILL_DUE_7D = Rule("finance.bill_due_7d", "finance", "high", "reply", "finance.ap_queue", _bill_due_7d)
R_CASH_LOW = Rule("finance.cash_low", "finance", "high", "reply", "finance.runway_alert", _cash_low)
R_UNMATCHED_INFLOW = Rule(
    "finance.unmatched_inflow", "finance", "low", "open", "finance.match_payer", _unmatched_inflow
)
R_ASK_UNTOUCHED = Rule(
    "customers.ask_untouched", "customers", "high", "reply", "customers.prioritize_ask", _ask_untouched
)
R_REPLY_DETECTED = Rule("sales.reply_detected", "sales", "high", "reply", "sales.draft_reply", _reply_detected)
R_STALE_DRAFT_CAMPAIGN = Rule(
    "sales.stale_draft_campaign", "marketing", "medium", "open", "sales.campaign_wave", _stale_draft_campaign
)
R_ANALYTICS_OFF = Rule("marketing.analytics_off", "web", "low", "click", "marketing.enable_analytics", _analytics_off)
R_QUEUE_BACKLOG = Rule("social.queue_backlog", "social", "medium", "click", "social.queue_batch", _queue_backlog)

RULES: tuple[Rule, ...] = (
    R_UNASSIGNED_HIGH,
    R_STALE_IN_PROGRESS,
    R_DUPLICATE_TITLES,
    R_DEPLOY_FAILED,
    R_BILL_DUE_7D,
    R_CASH_LOW,
    R_UNMATCHED_INFLOW,
    R_ASK_UNTOUCHED,
    R_REPLY_DETECTED,
    R_STALE_DRAFT_CAMPAIGN,
    R_ANALYTICS_OFF,
    R_QUEUE_BACKLOG,
)
RULES_BY_ID = {r.id: r for r in RULES}


def evaluate_all(ctx: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """Run every rule; result is sorted by id and de-duplicated (first wins) so it is deterministic."""
    seen: dict[str, dict[str, Any]] = {}
    for rule in RULES:
        for sig in rule.evaluate(ctx, now):
            seen.setdefault(sig["id"], sig)
    return [seen[k] for k in sorted(seen)]
