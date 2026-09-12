"""Finance: BREX_SNAPSHOT-shaped read of accounts_bank, bills, vendors, cards, transactions. Read-only, no bank details."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import psycopg
from fastapi import APIRouter, Depends

from api.deps import connection_sync_at, get_db, get_tenant, iso, money, money_ccy, now_utc

router = APIRouter(prefix="/finance", tags=["finance"])

OPEN_PAYMENT_STATUSES_SQL = (
    "coalesce(payment_status, '') NOT IN ('CLEARED', 'SETTLED') AND coalesce(status, '') NOT IN ('CANCELED', 'VOID')"
)


def _accounts(conn: psycopg.Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM accounts_bank WHERE tenant_id = %s ORDER BY (priority = 'PRIMARY') DESC, id", (tenant_id,)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "nickname": r["nickname"],
            "account_number_last_four": r["last4"],
            "account_type": r["account_type"],
            "priority": r["priority"],
            "balance_breakdown": {
                "available_balance": money_ccy(r["available"]),
                "processing_balance": money_ccy(0),
            },
            "cashflow": {
                "current_month_cash_inflows": money_ccy(r["inflow_mtd"]),
                "current_month_cash_outflows": money_ccy(r["outflow_mtd"]),
            },
            "observed_at": iso(r["observed_at"]),
        }
        for r in rows
    ]


def _bills(conn: psycopg.Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM bills WHERE tenant_id = %s ORDER BY coalesce(due_at, purchased_at) DESC NULLS LAST, id",
        (tenant_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "status": r["status"],
            "payment_status": r["payment_status"],
            "purchased_at": iso(r["purchased_at"]),
            "due_at": iso(r["due_at"]),
            "payment_send_at": iso(r["payment_send_at"]),
            "vendor_id": r["vendor_id"],
            "vendor_name": r["vendor_name"],
            "amount": money(r["amount"]),
            "currency": r["currency"],
            "external_invoice_number": r["invoice_number"],
            "memo": r["memo"],
        }
        for r in rows
    ]


def _vendors(conn: psycopg.Connection, tenant_id: str) -> list[dict[str, Any]]:
    # Only these six fields, ever. No bank details exist in the table and none are returned.
    rows = conn.execute(
        "SELECT id, name, status, email, rail, country FROM vendors WHERE tenant_id = %s ORDER BY name", (tenant_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _cards(conn: psycopg.Connection, tenant_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM cards WHERE tenant_id = %s ORDER BY id", (tenant_id,)).fetchall()
    out = []
    for r in rows:
        total = Decimal(r["limit_total"] or 0)
        spent = Decimal(r["limit_spent"] or 0)
        out.append(
            {
                "id": r["id"],
                "status": r["status"],
                "holder_name": r["holder"],
                "display_name": r["display_name"],
                "last4": r["last4"],
                "limit": {
                    "spent": {"quantity": money(spent), "instrument_code_string": "USD"},
                    "available": {"quantity": money(total - spent)},
                    "total": {"quantity": money(total)},
                },
            }
        )
    return out


def _transactions(conn: psycopg.Connection, tenant_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM transactions WHERE tenant_id = %s ORDER BY occurred_at DESC LIMIT %s", (tenant_id, limit)
    ).fetchall()
    return [
        {
            "id": r["id"],
            "type": r["type"],
            "status": r["status"],
            "amount": money_ccy(r["amount"], r["currency"]),
            "timestamp": iso(r["occurred_at"]),
            "display_name": r["counterparty"],
        }
        for r in rows
    ]


@router.get("")
def finance_snapshot(
    conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)
) -> dict[str, Any]:
    return {
        "snapshot_at": iso(connection_sync_at(conn, tenant_id, ("brex",))),
        "live": False,
        "accounts": _accounts(conn, tenant_id),
        "bills": _bills(conn, tenant_id),
        "vendors": _vendors(conn, tenant_id),
        "cards": _cards(conn, tenant_id),
        "expenses": [],
        "transactions": _transactions(conn, tenant_id),
    }


def finance_summary_data(conn: psycopg.Connection, tenant_id: str) -> dict[str, Any]:
    now = now_utc()
    cash = conn.execute(
        "SELECT coalesce(sum(available), 0) AS v FROM accounts_bank WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()["v"]
    ap = conn.execute(
        f"""SELECT coalesce(sum(amount), 0) AS v, count(*) AS n FROM bills
             WHERE tenant_id = %s AND {OPEN_PAYMENT_STATUSES_SQL} AND due_at IS NOT NULL AND due_at <= %s""",
        (tenant_id, now + timedelta(days=7)),
    ).fetchone()
    mtd = conn.execute(
        """SELECT coalesce(sum(amount), 0) AS v, count(*) AS n FROM transactions
            WHERE tenant_id = %s AND amount > 0 AND occurred_at >= date_trunc('month', %s::timestamptz)""",
        (tenant_id, now),
    ).fetchone()
    last_in = conn.execute(
        "SELECT max(occurred_at) AS ts FROM transactions WHERE tenant_id = %s AND amount > 0", (tenant_id,)
    ).fetchone()["ts"]
    outflow = conn.execute(
        "SELECT coalesce(sum(outflow_mtd), 0) AS v FROM accounts_bank WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()["v"]
    n_accounts = conn.execute("SELECT count(*) AS n FROM accounts_bank WHERE tenant_id = %s", (tenant_id,)).fetchone()[
        "n"
    ]
    return {
        "cash_on_hand": money(cash),
        "ap_next_7d": money(ap["v"]),
        "ap_next_7d_count": ap["n"],
        "received_mtd": money(mtd["v"]),
        "received_mtd_count": mtd["n"],
        "outflow_mtd": money(outflow),
        "last_inflow_at": iso(last_in),
        "accounts": n_accounts,
        "currency": "USD",
    }


@router.get("/summary")
def finance_summary(conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)) -> dict[str, Any]:
    return finance_summary_data(conn, tenant_id)
