"""signals.lookahead — pure Tier-0 facts: bills vs cash date, runway, milestones, deploy cadence, stale approvals."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from signals import lookahead

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _bill(bid, amount, due_days, **kw):
    return {
        "id": bid,
        "vendor_name": kw.get("vendor", "Vendor"),
        "amount": Decimal(amount),
        "currency": "USD",
        "status": kw.get("status", "APPROVED"),
        "payment_status": kw.get("payment_status", "AWAITING_PAYMENT"),
        "due_at": NOW + timedelta(days=due_days),
    }


PRIMARY = {"id": "acc_p", "priority": "PRIMARY", "available": Decimal("10000.00"), "outflow_mtd": Decimal("4000.00")}
VAULT = {"id": "acc_v", "priority": "NON_PRIMARY", "available": Decimal("99999.00"), "outflow_mtd": Decimal("0")}


def test_bills_vs_cash_finds_the_date_cash_goes_negative():
    bills = [
        _bill("b3", "6000.00", 10),
        _bill("b1", "3000.00", 2),
        _bill("b2", "5000.00", 6),
        _bill("b_far", "100.00", 20),  # outside 14d
        _bill("b_paid", "9999.00", 1, payment_status="CLEARED"),  # settled
        _bill("b_over", "500.00", -3),  # overdue counts
    ]
    out = lookahead.bills_vs_cash(bills, [VAULT, PRIMARY], NOW)
    assert out["account_id"] == "acc_p" and out["available"] == Decimal("10000.00")
    assert [b["id"] for b in out["bills_due"]] == ["b_over", "b1", "b2", "b3"]
    assert out["bills_due"][0]["days"] == -3 and out["bills_due"][1]["due"] == "2026-09-06"
    assert out["total_due"] == Decimal("14500.00") and out["after_paying_all"] == Decimal("-4500.00")
    # 10000 - 500 - 3000 - 5000 = 1500 ; - 6000 → negative on b3's due date
    assert out["cash_negative_on"] == "2026-09-14" and out["cash_negative_bill"] == "b3"


def test_bills_vs_cash_without_account_or_shortfall():
    out = lookahead.bills_vs_cash([_bill("b1", "10.00", 1)], [], NOW)
    assert out["available"] is None and out["cash_negative_on"] is None and out["bills_due"][0]["id"] == "b1"
    out = lookahead.bills_vs_cash([_bill("b1", "10.00", 1)], [PRIMARY], NOW)
    assert out["cash_negative_on"] is None and out["after_paying_all"] == Decimal("9990.00")


def test_runway_prefers_90d_transactions_then_outflow_mtd_then_none():
    tx = [
        {"id": "t1", "amount": Decimal("-3000.00"), "occurred_at": NOW - timedelta(days=10)},
        {"id": "t2", "amount": Decimal("-3000.00"), "occurred_at": NOW - timedelta(days=40)},
        {"id": "t3", "amount": Decimal("5000.00"), "occurred_at": NOW - timedelta(days=5)},  # inflow ignored
        {"id": "t4", "amount": Decimal("-9000.00"), "occurred_at": NOW - timedelta(days=200)},  # too old
    ]
    r = lookahead.runway([PRIMARY], tx, NOW)
    assert r["basis"] == "transactions_90d" and r["monthly_outflow"] == Decimal("2000.00") and r["months"] == 5.0
    r = lookahead.runway([PRIMARY], [], NOW)
    assert r["basis"] == "outflow_mtd" and r["monthly_outflow"] == Decimal("4000.00") and r["months"] == 2.5
    r = lookahead.runway([{"id": "a", "priority": "PRIMARY", "available": "100", "outflow_mtd": "0"}], [], NOW)
    assert r["months"] is None and r["basis"] == "no data"
    assert lookahead.runway([], [], NOW)["months"] is None


def _issue(iid, **kw):
    base = {
        "id": iid,
        "title": kw.get("title", iid),
        "status": kw.get("status", "Backlog"),
        "status_type": kw.get("status_type", "backlog"),
        "priority": kw.get("priority", 3),
        "assignee": kw.get("assignee"),
        "labels": kw.get("labels", []),
        "updated_at": NOW - timedelta(days=kw.get("idle", 0)),
        "raw": kw.get("raw", {}),
    }
    return base


def test_issues_due_reads_raw_due_date_and_skips_done():
    issues = [
        _issue("A-1", raw={"dueDate": "2026-09-06"}),
        _issue("A-2", raw={"dueDate": "2026-09-30"}),  # beyond 7d
        _issue("A-3", raw={"dueDate": "2026-09-01"}),  # overdue → included
        _issue("A-4", raw={"dueDate": "2026-09-05"}, status_type="completed"),
        _issue("A-5", raw=json.dumps({"dueDate": "2026-09-08T00:00:00Z"})),  # raw as a JSON string
        _issue("A-6", raw={"dueDate": "not a date"}),
    ]
    out = lookahead.issues_due(issues, NOW)
    assert [(i["id"], i["days"]) for i in out] == [("A-3", -3), ("A-1", 2), ("A-5", 4)]


def test_issues_untouched_only_urgent_high_open_idle_5d():
    issues = [
        _issue("U-1", priority=1, idle=6, status="In Progress", status_type="started"),
        _issue("U-2", priority=2, idle=5),
        _issue("U-3", priority=2, idle=4),
        _issue("U-4", priority=3, idle=30),
        _issue("U-5", priority=1, idle=30, status_type="completed"),
    ]
    out = lookahead.issues_untouched(issues, NOW)
    assert [(i["id"], i["priority"], i["idle_days"]) for i in out] == [("U-1", "urgent", 6), ("U-2", "high", 5)]


CUSTOMERS_BLOCK = """---
slice: customers
updated: 2026-09-04
milestones:
  - account: jci
    milestone: POC readout
    due: 2026-09-12
  - account: guidewire
    milestone: "Security review"
    due: 2026-10-30
  - account: broken
    milestone: no due date
source: human
---
# Customers
"""

CUSTOMERS_INLINE = (
    '---\nslice: customers\nmilestones: [{"account": "JCI", "milestone": "POC readout", "due": "2026-09-12"}]\n---\n'
)


def test_parse_milestones_block_and_inline_forms():
    got = lookahead.parse_milestones(CUSTOMERS_BLOCK)
    assert got == [
        {"account": "jci", "milestone": "POC readout", "due": "2026-09-12"},
        {"account": "guidewire", "milestone": "Security review", "due": "2026-10-30"},
    ]
    assert lookahead.parse_milestones(CUSTOMERS_INLINE) == [
        {"account": "JCI", "milestone": "POC readout", "due": "2026-09-12"}
    ]
    assert lookahead.parse_milestones("# no front matter\n") == []
    assert lookahead.parse_milestones("---\nslice: customers\n---\nbody") == []


def test_poc_milestones_within_14d_with_open_customer_issues():
    milestones = lookahead.parse_milestones(CUSTOMERS_BLOCK)
    issues = [
        _issue("C-1", labels=["customer:jci"]),
        _issue("C-2", labels=["account:JCI"], status_type="completed"),
        _issue("C-3", labels=["customer:guidewire"]),
        _issue("C-4", labels=["Bug"]),
    ]
    out = lookahead.poc_milestones(milestones, issues, NOW)
    assert out == [
        {"account": "jci", "milestone": "POC readout", "due": "2026-09-12", "days": 8, "open_issues": ["C-1"]}
    ]


def test_deploy_cadence_last_vs_prior_30d_production_only():
    def dep(i, days_ago, state="READY", target="production"):
        return {"id": f"d{i}", "state": state, "target": target, "created_at": NOW - timedelta(days=days_ago)}

    deps = [
        dep(1, 2),
        dep(2, 10, "ERROR"),
        dep(3, 40),
        dep(4, 50),
        dep(5, 55),
        dep(6, 100),
        dep(7, 1, target="preview"),
    ]
    out = lookahead.deploy_cadence(deps, NOW)
    assert out == {
        "last_30d": 2,
        "prior_30d": 3,
        "failed_last_30d": 1,
        "trend": "down",
        "last_prod_deploy": "2026-09-02",
        "days_since_last_prod_deploy": 2,
    }
    assert lookahead.deploy_cadence([], NOW)["trend"] == "no data"
    assert lookahead.deploy_cadence([dep(1, 2), dep(2, 40)], NOW)["trend"] == "flat"


def test_stale_approvals_older_than_48h():
    rows = [
        {
            "id": "a1",
            "module": "build",
            "type": "Linear · assign",
            "target": "X",
            "status": "pending",
            "created_at": NOW - timedelta(hours=49),
        },
        {
            "id": "a2",
            "module": "build",
            "type": "t",
            "target": "Y",
            "status": "pending",
            "created_at": NOW - timedelta(hours=47),
        },
        {
            "id": "a3",
            "module": "build",
            "type": "t",
            "target": "Z",
            "status": "executed",
            "created_at": NOW - timedelta(days=9),
        },
        {
            "id": "a4",
            "module": "finance",
            "type": "Brex · pay",
            "target": "W",
            "status": "pending",
            "created_at": NOW - timedelta(days=5),
        },
    ]
    out = lookahead.stale_approvals(rows, NOW)
    assert [(a["id"], a["age_hours"]) for a in out] == [("a4", 120), ("a1", 49)]


def test_campaign_ages_budget_and_sources():
    gtm = {"campaigns": [{"id": "w2", "name": "Wave 2", "status": "Draft", "since": "2026-06-03", "targets": 18}]}
    seqs = [{"id": "s1", "name": "VP Eng", "contacts": 42, "replied": 1, "observed_at": NOW - timedelta(days=3)}]
    g = lookahead.campaign_ages(gtm, seqs, NOW)
    assert g["campaigns"][0]["age_days"] == 93 and g["campaigns"][0]["status"] == "draft"
    assert g["sequences"][0]["observed_age_days"] == 3
    b = lookahead.budget_state(
        {
            "month": NOW.date().replace(day=1),
            "state": "conserve",
            "tier2_tokens_used": 900,
            "tier2_tokens_allowed": 1000,
            "cost_usd": "1.5",
        }
    )
    assert b["pct"] == 90.0 and b["month"] == "2026-09-01" and b["state"] == "conserve"
    assert lookahead.budget_state(None)["state"] == "no data"
    assert lookahead.disconnected_sources(
        [
            {"source": "vercel", "status": "needs_reauth"},
            {"source": "linear", "status": "connected"},
            {"source": "brex"},
        ]
    ) == [{"source": "vercel", "status": "needs_reauth", "last_error": None}]


def test_facts_is_json_serializable_stable_and_uses_only_now():
    ctx = {
        "bills": [_bill("b1", "12000.00", 3)],
        "accounts_bank": [PRIMARY],
        "issues": [_issue("U-1", priority=1, idle=9)],
        "deployments": [],
        "customers_md": CUSTOMERS_BLOCK,
        "connections": [{"source": "vercel", "status": "disabled"}],
        "approvals": [],
        "budget": None,
    }
    a = lookahead.facts(ctx, NOW)
    b = lookahead.facts(ctx, NOW)
    assert a == b and json.dumps(a) == json.dumps(b)
    assert list(a) == sorted(a)  # stable key order
    assert a["cash"]["cash_negative_on"] == "2026-09-07" and a["cash"]["available"] == "10000.00"
    assert a["runway"]["months"] == 2.5 and a["milestones"][0]["account"] == "jci"
    heads = lookahead.headlines(a)
    assert heads[0]["title"].startswith("Cash goes negative on 2026-09-07") and heads[0]["module"] == "finance"
    assert {h["module"] for h in heads} == {"finance", "customers", "build"} and len(heads) == 5
    assert any(h["module"] == "security" and "vercel" in h["title"] for h in lookahead.headlines(a, limit=10))
