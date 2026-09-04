"""Fixture → typed row mapping for every source (no Postgres). Values come only from tests/fixtures."""

from datetime import UTC, datetime
from decimal import Decimal

from ingest import brex, linear, slack, vercel
from ingest.brex import VENDOR_FIELDS


def test_linear_issue_mapping():
    issues, projects = linear.from_fixture()
    by_id = {i["id"]: i for i in issues}
    assert len(issues) == 9 and len(projects) == 3
    acm158 = by_id["ACM-158"]
    assert acm158["priority"] == 2 and acm158["assignee"] is None and acm158["status_type"] == "backlog"
    assert acm158["labels"] == ["Improvement"]
    assert acm158["updated_at"] == datetime(2026, 9, 3, 11, 23, 49, 100000, tzinfo=UTC)
    assert by_id["ACM-160"]["labels"] == ["customer:northwind"] and by_id["ACM-160"]["project"] == "Customer POC"
    assert by_id["ACM-137"]["status_type"] == "completed"
    assert by_id["ACM-126"]["priority"] == 1


def test_linear_graphql_shape_maps_like_fixture():
    node = {
        "id": "uuid-1",
        "identifier": "ACM-158",
        "title": "workspace-graph build is 24 min per parent; corpus scale infeasible",
        "priority": 2,
        "url": "https://linear.app/acme/issue/ACM-158",
        "createdAt": "2026-09-02T11:00:00.000Z",
        "updatedAt": "2026-09-03T11:23:49.100Z",
        "state": {"name": "Backlog", "type": "backlog"},
        "assignee": None,
        "project": None,
        "team": {"name": "Acme"},
        "labels": {"nodes": [{"name": "Improvement"}]},
    }
    row = linear.normalize_issue_graphql(node)
    fixture_row = {i["id"]: i for i in linear.from_fixture()[0]}["ACM-158"]
    for col in ("id", "title", "status", "status_type", "priority", "assignee", "team", "labels", "url", "updated_at"):
        assert row[col] == fixture_row[col], col
    assert row["uuid"] == "uuid-1"


def test_linear_projects():
    projects = {p["id"]: p for p in linear.from_fixture()[1]}
    assert projects["p3"]["lead"] == "Alex Dev" and projects["p3"]["status"] == "In Progress"
    assert projects["p3"]["target_date"].isoformat() == "2026-12-31"
    assert projects["p1"]["lead"] is None and projects["p1"]["target_date"] is None


def test_brex_accounts_and_bills():
    data = brex.from_fixture()
    primary = {a["id"]: a for a in data["accounts_bank"]}["dpacc_primary"]
    assert primary["available"] == Decimal("8517.26") and primary["priority"] == "PRIMARY"
    assert primary["outflow_mtd"] == Decimal("6500.00") and primary["last4"] == "1234"
    b2 = {b["id"]: b for b in data["bills"]}["expense_b2"]
    assert b2["amount"] == Decimal("6500.00") and b2["currency"] == "USD"
    assert b2["payment_status"] == "AWAITING_PAYMENT" and b2["invoice_number"] == "INV-2"
    assert b2["due_at"] == datetime(2026, 9, 9, 12, tzinfo=UTC) and b2["payment_send_at"] is None


def test_brex_vendors_store_only_allowed_fields():
    vendors = brex.from_fixture()["vendors"]
    assert len(vendors) == 2
    for v in vendors:
        assert set(v) == set(VENDOR_FIELDS)
        assert not any(k in v for k in ("account_number", "routing_number", "tax_id", "bank"))
    v2 = {v["id"]: v for v in vendors}["pdcont_v2"]
    assert v2 == {
        "id": "pdcont_v2",
        "name": "DevShop LLC",
        "email": "ap@devshop.example",
        "rail": "INTL_SWIFT_WIRE",
        "country": "UA",
        "status": "ACTIVE",
    }


def test_brex_cards_and_transactions():
    data = brex.from_fixture()
    card = data["cards"][0]
    assert card["id"] == "ncard_1" and card["last4"] == "6445" and card["holder"] == "Cofounder"
    assert card["limit_total"] == Decimal("100000.00") and card["limit_spent"] == Decimal("0.00")
    tx = {t["id"]: t for t in data["transactions"]}
    assert tx["dptx_1"]["amount"] == Decimal("2500.00")  # incoming → positive
    assert tx["dptx_1"]["counterparty"] == "FOUNDER CAPITAL"
    assert tx["dptx_2"]["occurred_at"] == datetime(2026, 6, 16, 14, 37, 29, tzinfo=UTC)


def test_brex_outgoing_transaction_is_negative():
    row = brex.normalize_transaction(
        {
            "id": "dptx_out",
            "type": "ACH",
            "status": "PROCESSED",
            "amount": "517.26 USD",
            "timestamp": "2026-08-01T00:00:00Z",
            "source_account": {"account_type": "BREX_BUSINESS_ACCOUNT", "id": "dpacc_primary"},
            "target_account": {"account_type": "EXTERNAL_ACCOUNT", "display_name": "DevShop LLC"},
        }
    )
    assert row["amount"] == Decimal("-517.26") and row["counterparty"] == "DevShop LLC"


def test_slack_messages():
    rows = slack.from_fixture()
    assert len(rows) == 3
    by_id = {r["id"]: r for r in rows}
    m = by_id["1756800000.000100"]
    assert m["channel"] == "C0SALES" and m["author"] == "U1" and m["source"] == "slack"
    assert m["text"].startswith("Northwind replied")
    assert m["occurred_at"] == datetime.fromtimestamp(1756800000.0001, tz=UTC)
    assert m["thread_id"] is None


def test_slack_skips_joins_and_empty():
    assert slack.normalize_message({"ts": "1.0", "subtype": "channel_join", "text": "joined"}, "C") is None
    assert slack.normalize_message({"ts": "1.0", "text": "  "}, "C") is None
    assert slack.normalize_message({"text": "no ts"}, "C") is None


def test_vercel_deployments():
    rows = {d["id"]: d for d in vercel.from_fixture()}
    assert rows["dpl_2"]["state"] == "ERROR" and rows["dpl_2"]["target"] == "production"
    assert rows["dpl_2"]["commit_message"] == "feat: add demo" and rows["dpl_2"]["project"] == "site"
    assert rows["dpl_2"]["created_at"].date().isoformat() == "2026-09-01"
    assert rows["dpl_1"]["url"] == "https://vercel.com/acme/site/1"
