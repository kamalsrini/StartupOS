"""Every rule: one positive and one negative case over a hand-built ctx. Values mirror tests/fixtures."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from signals import rules
from signals.engine import build_ctx, evaluate

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def issue(**over):
    base = {
        "id": "ACM-158",
        "title": "workspace-graph build is 24 min per parent; corpus scale infeasible",
        "status": "Backlog",
        "status_type": "backlog",
        "priority": 2,
        "assignee": None,
        "labels": [],
        "url": "https://linear.app/acme/issue/ACM-158",
        "updated_at": NOW - timedelta(days=1),
    }
    return {**base, **over}


def ids(signals):
    return sorted(s["id"] for s in signals)


# --- build.unassigned_high ------------------------------------------------------------


def test_unassigned_high_fires_for_open_high_without_assignee():
    out = rules.R_UNASSIGNED_HIGH.evaluate(build_ctx(issues=[issue()]), NOW)
    assert ids(out) == ["build.unassigned_high:ACM-158"]
    sig = out[0]
    assert sig["module"] == "build" and sig["severity"] == "high" and sig["kind"] == "click"
    assert sig["suggested_skill"] == "build.assign_owner" and sig["entity"] == "issues"
    assert sig["href"] == "https://linear.app/acme/issue/ACM-158"


def test_unassigned_high_negative_cases():
    assigned = issue(assignee="Alex Dev")
    low = issue(id="ACM-159", priority=0)
    done = issue(id="ACM-137", status="Done", status_type="completed")
    assert rules.R_UNASSIGNED_HIGH.evaluate(build_ctx(issues=[assigned, low, done]), NOW) == []


# --- build.stale_in_progress ----------------------------------------------------------


def test_stale_in_progress_fires_after_7_days():
    stale = issue(id="ACM-126", status="In Progress", status_type="started", updated_at=NOW - timedelta(days=15))
    out = rules.R_STALE_IN_PROGRESS.evaluate(build_ctx(issues=[stale]), NOW)
    assert ids(out) == ["build.stale_in_progress:ACM-126"]
    assert "15 days" in out[0]["meta"] and out[0]["severity"] == "medium"


def test_stale_in_progress_negative_cases():
    fresh = issue(id="ACM-156", status="In Progress", status_type="started", updated_at=NOW - timedelta(days=1))
    old_backlog = issue(id="ACM-150", status_type="backlog", updated_at=NOW - timedelta(days=30))
    assert rules.R_STALE_IN_PROGRESS.evaluate(build_ctx(issues=[fresh, old_backlog]), NOW) == []


# --- build.duplicate_titles -----------------------------------------------------------


def test_duplicate_titles_groups_open_issues():
    a = issue(id="ACM-151", title="Investigate Agent Identity Threat", priority=0)
    b = issue(id="ACM-150", title="investigate  agent identity threat!", priority=0)
    out = rules.R_DUPLICATE_TITLES.evaluate(build_ctx(issues=[a, b]), NOW)
    assert ids(out) == ["build.duplicate_titles:ACM-150+ACM-151"]
    assert out[0]["module"] == "security" and out[0]["kind"] == "reply"


def test_duplicate_titles_ignores_closed_and_distinct():
    a = issue(id="ACM-151", title="Investigate Agent Identity Threat", priority=0)
    closed = issue(id="ACM-150", title="Investigate Agent Identity Threat", priority=0, status_type="completed")
    other = issue(id="ACM-161", title="fixer picks files_changed[0] as primary; wrong main file")
    assert rules.R_DUPLICATE_TITLES.evaluate(build_ctx(issues=[a, closed, other]), NOW) == []


# --- build.deploy_failed --------------------------------------------------------------


def deployment(**over):
    base = {
        "id": "dpl_2",
        "project": "site",
        "state": "ERROR",
        "target": "production",
        "commit_message": "feat: add demo",
        "created_at": NOW - timedelta(days=2),
        "url": "https://vercel.com/acme/site/2",
    }
    return {**base, **over}


def test_deploy_failed_recent_error():
    out = rules.R_DEPLOY_FAILED.evaluate(build_ctx(deployments=[deployment()]), NOW)
    assert ids(out) == ["build.deploy_failed:dpl_2"]
    assert out[0]["module"] == "web" and out[0]["kind"] == "bounce"


def test_deploy_failed_window_is_anchored_to_now_not_the_feed():
    # PE review 2026-09-04: an ERROR from months ago is history, not a signal, even if nothing deployed since.
    old_error = deployment(created_at=NOW - timedelta(days=154))
    newer_ok = deployment(id="dpl_1", state="READY", created_at=NOW - timedelta(days=150))
    assert ids(rules.R_DEPLOY_FAILED.evaluate(build_ctx(deployments=[old_error, newer_ok]), NOW)) == []


def test_deploy_failed_negative_cases():
    ready = deployment(state="READY")
    aged_out = deployment(id="dpl_0", created_at=NOW - timedelta(days=20))
    recent_ok = deployment(id="dpl_3", state="READY", created_at=NOW - timedelta(hours=1))
    assert rules.R_DEPLOY_FAILED.evaluate(build_ctx(deployments=[ready, aged_out, recent_ok]), NOW) == []
    assert rules.R_DEPLOY_FAILED.evaluate(build_ctx(), NOW) == []


# --- finance.bill_due_7d --------------------------------------------------------------


def bill(**over):
    base = {
        "id": "expense_b2",
        "vendor_name": "DevShop LLC",
        "amount": Decimal("6500.00"),
        "currency": "USD",
        "status": "SUBMITTED",
        "payment_status": "AWAITING_PAYMENT",
        "due_at": datetime(2026, 9, 9, 12, tzinfo=UTC),
    }
    return {**base, **over}


def test_bill_due_7d_positive():
    out = rules.R_BILL_DUE_7D.evaluate(build_ctx(bills=[bill()]), NOW)
    assert ids(out) == ["finance.bill_due_7d:expense_b2"]
    assert out[0]["severity"] == "high" and "due in 5d" in out[0]["meta"]
    assert out[0]["href"].endswith("/bills/expense_b2")


def test_bill_due_7d_overdue_still_fires():
    out = rules.R_BILL_DUE_7D.evaluate(build_ctx(bills=[bill(due_at=NOW - timedelta(days=3))]), NOW)
    assert len(out) == 1 and out[0]["meta"].startswith("overdue")


def test_bill_due_7d_negative_cases():
    cleared = bill(id="expense_b1", payment_status="CLEARED", due_at=datetime(2026, 6, 11, 12, tzinfo=UTC))
    later = bill(id="expense_b3", payment_status="SCHEDULED", due_at=datetime(2026, 9, 20, 12, tzinfo=UTC))
    settled = bill(id="expense_b4", status="SETTLED", payment_status=None)
    no_due = bill(id="expense_b5", due_at=None)
    assert rules.R_BILL_DUE_7D.evaluate(build_ctx(bills=[cleared, later, settled, no_due]), NOW) == []


# --- finance.cash_low -----------------------------------------------------------------


def account(**over):
    base = {
        "id": "dpacc_primary",
        "name": "Acme, Inc. checking account",
        "nickname": "Primary checking",
        "priority": "PRIMARY",
        "available": Decimal("8517.26"),
        "inflow_mtd": Decimal("2500.00"),
        "outflow_mtd": Decimal("6500.00"),
    }
    return {**base, **over}


def test_cash_low_uses_mtd_outflow_when_no_outgoing_transactions():
    out = rules.R_CASH_LOW.evaluate(build_ctx(accounts_bank=[account()]), NOW)
    assert ids(out) == ["finance.cash_low:dpacc_primary"]
    assert "1.3 months runway" in out[0]["meta"] and out[0]["severity"] == "high"


def test_cash_low_uses_transaction_average_when_present():
    txs = [
        {"id": f"t{i}", "amount": Decimal("-3000.00"), "occurred_at": NOW - timedelta(days=10 * (i + 1))}
        for i in range(3)
    ]
    # 9000 over 90 days → 3000/month → threshold 9000 → 8517.26 is below
    out = rules.R_CASH_LOW.evaluate(build_ctx(accounts_bank=[account()], transactions=txs), NOW)
    assert len(out) == 1 and "3,000.00" in out[0]["meta"]


def test_cash_low_configured_floor():
    ctx = build_ctx(accounts_bank=[account(available=Decimal("25000.00"))], config={"cash_floor": Decimal("30000")})
    out = rules.R_CASH_LOW.evaluate(ctx, NOW)
    assert len(out) == 1 and "configured floor" in out[0]["meta"]


def test_cash_low_negative_cases():
    healthy = account(available=Decimal("25000.00"))
    assert rules.R_CASH_LOW.evaluate(build_ctx(accounts_bank=[healthy]), NOW) == []
    vault_only = account(id="dpacc_vault", priority="NON_PRIMARY", available=Decimal("0.00"))
    assert rules.R_CASH_LOW.evaluate(build_ctx(accounts_bank=[vault_only]), NOW) == []
    no_burn = account(outflow_mtd=Decimal("0.00"))
    assert rules.R_CASH_LOW.evaluate(build_ctx(accounts_bank=[no_burn]), NOW) == []


# --- finance.unmatched_inflow ---------------------------------------------------------


def inflow(**over):
    base = {
        "id": "dptx_2",
        "type": "WIRE",
        "amount": Decimal("9000.00"),
        "currency": "USD",
        "counterparty": "NORTHWIND TRADERS",
        "occurred_at": datetime(2026, 6, 16, 14, 37, 29, tzinfo=UTC),
    }
    return {**base, **over}


def test_unmatched_inflow_positive():
    out = rules.R_UNMATCHED_INFLOW.evaluate(build_ctx(transactions=[inflow()]), NOW)
    assert ids(out) == ["finance.unmatched_inflow:dptx_2"]
    assert out[0]["severity"] == "low" and out[0]["kind"] == "open"


def test_unmatched_inflow_negative_cases():
    matched_account = build_ctx(transactions=[inflow()], accounts=[{"id": "northwind", "name": "Northwind Traders"}])
    assert rules.R_UNMATCHED_INFLOW.evaluate(matched_account, NOW) == []
    matched_invoice = build_ctx(transactions=[inflow()], invoices=[{"counterparty": "NORTHWIND TRADERS"}])
    assert rules.R_UNMATCHED_INFLOW.evaluate(matched_invoice, NOW) == []
    outgoing = build_ctx(transactions=[inflow(amount=Decimal("-9000.00"))])
    assert rules.R_UNMATCHED_INFLOW.evaluate(outgoing, NOW) == []
    ancient = build_ctx(transactions=[inflow(occurred_at=NOW - timedelta(days=200))])
    assert rules.R_UNMATCHED_INFLOW.evaluate(ancient, NOW) == []


# --- customers.ask_untouched ----------------------------------------------------------


def test_ask_untouched_positive():
    ask = issue(
        id="ACM-160",
        title="JSON issue report creation for the fixes the customer sends",
        priority=0,
        assignee="Mani Dev",
        labels=["customer:northwind"],
        updated_at=datetime(2026, 9, 1, 17, 50, 10, tzinfo=UTC),
    )
    out = rules.R_ASK_UNTOUCHED.evaluate(build_ctx(issues=[ask]), NOW)
    assert ids(out) == ["customers.ask_untouched:ACM-160"]
    assert out[0]["module"] == "customers" and out[0]["severity"] == "high"
    assert out[0]["meta"].startswith("northwind · untouched 3 days")


def test_ask_untouched_negative_cases():
    fresh = issue(id="ACM-160", labels=["customer:northwind"], updated_at=NOW - timedelta(days=1))
    unlabelled = issue(id="ACM-158", labels=["Improvement"], updated_at=NOW - timedelta(days=10))
    done = issue(
        id="ACM-137", labels=["customer:northwind"], status_type="completed", updated_at=NOW - timedelta(days=10)
    )
    assert rules.R_ASK_UNTOUCHED.evaluate(build_ctx(issues=[fresh, unlabelled, done]), NOW) == []


# --- sales.reply_detected -------------------------------------------------------------


def test_reply_detected_positive():
    seqs = [{"id": "cra-ot-bas", "name": "CRA OT/BAS", "contacts": 763, "replied": 3}]
    events = [{"id": 1, "entity_id": "cra-ot-bas", "kind": "updated", "diff": {"replied": [1, 3]}}]
    out = rules.R_REPLY_DETECTED.evaluate(build_ctx(sequences=seqs, sequence_events=events), NOW)
    assert ids(out) == ["sales.reply_detected:cra-ot-bas"]
    assert out[0]["title"].startswith("2 new replies") and out[0]["severity"] == "high"


def test_reply_detected_created_event_counts_as_increase():
    events = [{"id": 1, "entity_id": "vp-eng", "kind": "created", "diff": {"replied": [None, 1]}}]
    out = rules.R_REPLY_DETECTED.evaluate(build_ctx(sequence_events=events), NOW)
    assert ids(out) == ["sales.reply_detected:vp-eng"]


def test_reply_detected_negative_cases():
    no_change = [{"id": 1, "entity_id": "vp-eng", "kind": "updated", "diff": {"opened": [10, 12]}}]
    decrease = [{"id": 2, "entity_id": "cto-v2", "kind": "updated", "diff": {"replied": [3, 3]}}]
    assert rules.R_REPLY_DETECTED.evaluate(build_ctx(sequence_events=no_change + decrease), NOW) == []


# --- sales.stale_draft_campaign -------------------------------------------------------


def test_stale_draft_campaign_positive():
    gtm = {
        "campaigns": [
            {
                "id": "jci-alts-wave-2",
                "name": "JCI Alts wave 2.0",
                "status": "draft",
                "since": "2026-06-03",
                "targets": 18,
            }
        ]
    }
    out = rules.R_STALE_DRAFT_CAMPAIGN.evaluate(build_ctx(gtm=gtm), NOW)
    assert ids(out) == ["sales.stale_draft_campaign:jci-alts-wave-2"]
    assert out[0]["module"] == "marketing" and "93 days" in out[0]["title"] and "18 scored targets" in out[0]["meta"]


def test_stale_draft_campaign_negative_cases():
    recent = {"id": "w3", "name": "Wave 3", "status": "draft", "since": (NOW - timedelta(days=5)).date().isoformat()}
    active = {"id": "w2", "name": "Wave 2", "status": "active", "since": "2026-06-03"}
    assert rules.R_STALE_DRAFT_CAMPAIGN.evaluate(build_ctx(gtm={"campaigns": [recent, active]}), NOW) == []
    assert rules.R_STALE_DRAFT_CAMPAIGN.evaluate(build_ctx(gtm={}), NOW) == []


# --- marketing.analytics_off ----------------------------------------------------------


def test_analytics_off_positive():
    conns = [
        {"source": "linear", "status": "connected", "config": {}},
        {"source": "vercel", "status": "connected", "config": {}},
    ]
    out = rules.R_ANALYTICS_OFF.evaluate(build_ctx(connections=conns), NOW)
    assert ids(out) == ["marketing.analytics_off:posthog"]
    assert out[0]["module"] == "web" and out[0]["severity"] == "low"


def test_analytics_off_negative_cases():
    posthog = [{"source": "posthog", "status": "connected", "config": {}}]
    assert rules.R_ANALYTICS_OFF.evaluate(build_ctx(connections=posthog), NOW) == []
    vercel_wa = [{"source": "vercel", "status": "connected", "config": {"analytics": True}}]
    assert rules.R_ANALYTICS_OFF.evaluate(build_ctx(connections=vercel_wa), NOW) == []


# --- social.queue_backlog -------------------------------------------------------------


def test_queue_backlog_positive():
    out = rules.R_QUEUE_BACKLOG.evaluate(build_ctx(social={"linkedin_pending": 26}), NOW)
    assert ids(out) == ["social.queue_backlog:linkedin"]
    assert out[0]["module"] == "social" and out[0]["severity"] == "medium"


def test_queue_backlog_negative_cases():
    assert rules.R_QUEUE_BACKLOG.evaluate(build_ctx(social={"linkedin_pending": 25}), NOW) == []
    assert rules.R_QUEUE_BACKLOG.evaluate(build_ctx(), NOW) == []


# --- registry ---------------------------------------------------------------------------


def test_registry_matches_contract():
    expected = {
        "build.unassigned_high": ("build", "high", "click"),
        "build.stale_in_progress": ("build", "medium", "open"),
        "build.duplicate_titles": ("security", "medium", "reply"),
        "build.deploy_failed": ("web", "medium", "bounce"),
        "finance.bill_due_7d": ("finance", "high", "reply"),
        "finance.cash_low": ("finance", "high", "reply"),
        "finance.unmatched_inflow": ("finance", "low", "open"),
        "customers.ask_untouched": ("customers", "high", "reply"),
        "sales.reply_detected": ("sales", "high", "reply"),
        "sales.stale_draft_campaign": ("marketing", "medium", "open"),
        "marketing.analytics_off": ("web", "low", "click"),
        "social.queue_backlog": ("social", "medium", "click"),
    }
    assert {r.id: (r.module, r.severity, r.kind) for r in rules.RULES} == expected


def test_evaluate_is_sorted_and_signal_ids_follow_contract():
    ctx = build_ctx(issues=[issue(), issue(id="ACM-126", status_type="started", updated_at=NOW - timedelta(days=15))])
    out = evaluate(ctx, NOW)
    assert [s["id"] for s in out] == sorted(s["id"] for s in out)
    for s in out:
        assert s["id"] == f"{s['rule_id']}:{s['entity_id']}"
        assert set(s) >= {
            "id",
            "module",
            "rule_id",
            "severity",
            "kind",
            "title",
            "meta",
            "entity",
            "entity_id",
            "suggested_skill",
            "href",
        }
