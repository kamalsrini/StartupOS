"""Brex ingest (read-only): cash accounts, bills, vendors, cards, transactions.

Vendors are stored as id, name, email, rail, country, status ONLY. Bank account numbers, routing numbers,
tax ids and addresses are never read into StartupOS. StartupOS never moves money.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import psycopg

from common.settings import settings
from ingest import fixtures
from ingest.base import Upserter, UpsertResult, parse_money, parse_ts

log = logging.getLogger(__name__)

BASE_URL = "https://platform.brexapis.com"
KEY_REF = "env:BREX_API_TOKEN"

ENDPOINTS = {
    "accounts": "/v2/accounts/cash",
    "bills": "/v1/bills",
    "vendors": "/v1/vendors",
    "cards": "/v2/cards",
    "transactions": "/v2/transactions/cash/{account_id}",
}

accounts_upserter = Upserter(
    "accounts_bank",
    "brex",
    ["id"],
    ["id", "name", "nickname", "account_type", "priority", "last4", "available", "inflow_mtd", "outflow_mtd"],
)
bills_upserter = Upserter(
    "bills",
    "brex",
    ["id"],
    [
        "id",
        "vendor_id",
        "vendor_name",
        "amount",
        "currency",
        "status",
        "payment_status",
        "due_at",
        "purchased_at",
        "payment_send_at",
        "invoice_number",
        "memo",
        "raw",
    ],
    json_cols=["raw"],
)
vendors_upserter = Upserter("vendors", "brex", ["id"], ["id", "name", "email", "rail", "country", "status"])
cards_upserter = Upserter(
    "cards", "brex", ["id"], ["id", "holder", "display_name", "last4", "status", "limit_total", "limit_spent"]
)
transactions_upserter = Upserter(
    "transactions",
    "brex",
    ["id"],
    ["id", "type", "status", "amount", "currency", "counterparty", "occurred_at", "raw"],
    json_cols=["raw"],
)

VENDOR_FIELDS = ("id", "name", "email", "rail", "country", "status")  # the allow-list; nothing else leaves Brex


# --- normalizers -----------------------------------------------------------------


def normalize_account(raw: dict[str, Any]) -> dict[str, Any]:
    bb = raw.get("balance_breakdown") or {}
    cf = raw.get("cashflow") or {}
    available, _ = parse_money(bb.get("available_balance") or raw.get("current_balance"))
    inflow, _ = parse_money(cf.get("current_month_cash_inflows"))
    outflow, _ = parse_money(cf.get("current_month_cash_outflows"))
    return {
        "id": raw["id"],
        "name": raw.get("name"),
        "nickname": raw.get("nickname"),
        "account_type": raw.get("account_type"),
        "priority": raw.get("priority"),
        "last4": raw.get("account_number_last_four") or raw.get("last4"),
        "available": available,
        "inflow_mtd": inflow,
        "outflow_mtd": outflow,
    }


def normalize_bill(raw: dict[str, Any]) -> dict[str, Any]:
    amount, currency = parse_money(raw.get("amount"), raw.get("currency") or "USD")
    if raw.get("currency"):
        currency = raw["currency"]
    return {
        "id": raw["id"],
        "vendor_id": raw.get("vendor_id") or (raw.get("vendor") or {}).get("id"),
        "vendor_name": raw.get("vendor_name") or (raw.get("vendor") or {}).get("name"),
        "amount": amount,
        "currency": currency,
        "status": raw.get("status"),
        "payment_status": raw.get("payment_status"),
        "due_at": parse_ts(raw.get("due_at")),
        "purchased_at": parse_ts(raw.get("purchased_at")),
        "payment_send_at": parse_ts(raw.get("payment_send_at")),
        "invoice_number": raw.get("external_invoice_number") or raw.get("invoice_number"),
        "memo": raw.get("memo"),
        "raw": raw,
    }


def normalize_vendor(raw: dict[str, Any]) -> dict[str, Any]:
    rails = raw.get("rail_account_details") or []
    first = rails[0] if rails else {}
    rail = first.get("rail")
    if isinstance(rail, dict):
        rail = rail.get("type")
    row = {
        "id": raw["id"],
        "name": raw.get("name") or raw.get("company_name") or raw["id"],
        "email": raw.get("email"),
        "rail": rail,
        "country": first.get("target_country") or first.get("country"),
        "status": first.get("status") or raw.get("status"),
    }
    assert set(row) == set(VENDOR_FIELDS)
    return row


def normalize_card(raw: dict[str, Any]) -> dict[str, Any]:
    limit = raw.get("limit") or {}
    total = parse_money(limit.get("total"))[0] if limit.get("total") is not None else None
    spent = parse_money(limit.get("spent"))[0] if limit.get("spent") is not None else None
    user = raw.get("user") or {}
    holder = raw.get("holder_name") or " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return {
        "id": raw["id"],
        "holder": holder or None,
        "display_name": raw.get("display_name") or raw.get("card_name"),
        "last4": raw.get("last4") or raw.get("last_four"),
        "status": raw.get("status"),
        "limit_total": total,
        "limit_spent": spent,
    }


def normalize_transaction(raw: dict[str, Any]) -> dict[str, Any]:
    amount, currency = parse_money(raw.get("amount"))
    target = raw.get("target_account") or {}
    source = raw.get("source_account") or {}
    # Sign: + when money lands in a Brex business account, − when it leaves one.
    incoming = target.get("account_type") == "BREX_BUSINESS_ACCOUNT" or (
        not target and source.get("account_type") != "BREX_BUSINESS_ACCOUNT"
    )
    if source.get("account_type") == "BREX_BUSINESS_ACCOUNT" and target.get("account_type") != "BREX_BUSINESS_ACCOUNT":
        incoming = False
    signed = abs(amount) if incoming else -abs(amount)
    counterparty = raw.get("display_name") or (source.get("display_name") if incoming else target.get("display_name"))
    return {
        "id": raw["id"],
        "type": raw.get("type"),
        "status": raw.get("status"),
        "amount": signed,
        "currency": currency,
        "counterparty": counterparty,
        "occurred_at": parse_ts(raw.get("timestamp") or raw.get("posted_at_date") or raw.get("initiated_at_date")),
        "raw": raw,
    }


# --- sources -----------------------------------------------------------------------


def from_fixture(base: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    return {
        "accounts_bank": [normalize_account(a) for a in fixtures.load("brex_accounts.json", base)["items"]],
        "bills": [normalize_bill(b) for b in fixtures.load("brex_bills.json", base)["items"]],
        "vendors": [normalize_vendor(v) for v in fixtures.load("brex_vendors.json", base)["items"]],
        "cards": [normalize_card(c) for c in fixtures.load("brex_cards.json", base)["items"]],
        "transactions": [normalize_transaction(t) for t in fixtures.load("brex_transactions.json", base)["items"]],
    }


def _get_all(client: httpx.Client, token: str, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = client.get(BASE_URL + path, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30.0)
        resp.raise_for_status()
        payload = resp.json()
        items.extend(payload.get("items") or [])
        cursor = payload.get("next_cursor")
        if not cursor:
            return items


def fetch_live(token: str | None = None) -> dict[str, list[dict[str, Any]]]:
    tok = token or settings.secret(KEY_REF)
    if not tok:
        raise RuntimeError("BREX_API_TOKEN not configured")
    with httpx.Client() as client:
        accounts_raw = _get_all(client, tok, ENDPOINTS["accounts"])
        out = {
            "accounts_bank": [normalize_account(a) for a in accounts_raw],
            "bills": [normalize_bill(b) for b in _get_all(client, tok, ENDPOINTS["bills"])],
            "vendors": [normalize_vendor(v) for v in _get_all(client, tok, ENDPOINTS["vendors"])],
            "cards": [normalize_card(c) for c in _get_all(client, tok, ENDPOINTS["cards"])],
            "transactions": [],
        }
        for acct in accounts_raw:
            path = ENDPOINTS["transactions"].format(account_id=acct["id"])
            out["transactions"].extend(normalize_transaction(t) for t in _get_all(client, tok, path))
    return out


def has_credentials() -> bool:
    return bool(settings.secret(KEY_REF))


def sync(conn: psycopg.Connection, tenant_id: str, *, use_fixtures: bool = False) -> dict[str, UpsertResult]:
    data = from_fixture() if use_fixtures else fetch_live()
    return {
        "accounts_bank": accounts_upserter.upsert(conn, tenant_id, data["accounts_bank"]),
        "bills": bills_upserter.upsert(conn, tenant_id, data["bills"]),
        "vendors": vendors_upserter.upsert(conn, tenant_id, data["vendors"]),
        "cards": cards_upserter.upsert(conn, tenant_id, data["cards"]),
        "transactions": transactions_upserter.upsert(conn, tenant_id, data["transactions"]),
    }
