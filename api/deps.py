"""Shared dependencies and helpers for the API: DB connection, tenant scoping, money, cell sanitizing."""

from __future__ import annotations

import html
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
from fastapi import Header, Query

from common.db import get_conn
from common.models import Approval, Exec
from common.settings import settings


def api_dsn() -> str:
    """Tests point the API at the scratch DB via STARTUPOS_API_DSN; otherwise settings.database_url."""
    return os.environ.get("STARTUPOS_API_DSN") or settings.database_url


def get_db() -> Iterator[psycopg.Connection]:
    with get_conn(api_dsn()) as conn:
        yield conn


def get_tenant(
    tenant: str | None = Query(default=None, description="tenant slug; defaults to TENANT_ID"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> str:
    return tenant or x_tenant_id or settings.tenant_id


# --- money ------------------------------------------------------------------


def money(value: Decimal | float | int | None) -> str:
    """Decimal → '12500.00'. None → '0.00'. Never invents a number."""
    if value is None:
        return "0.00"
    return f"{Decimal(value):.2f}"


def money_ccy(value: Decimal | float | int | None, currency: str = "USD") -> str:
    """'517.26 USD' — the artifact's BREX_SNAPSHOT string shape."""
    return f"{money(value)} {currency}"


def compact_money(value: Decimal | float | int | None) -> str:
    """Tile-friendly: $0 → '$0', 2500 → '$2.5k', 1_250_000 → '$1.25M'."""
    d = Decimal(value or 0)
    n = abs(d)
    sign = "-" if d < 0 else ""
    if n >= 1_000_000:
        return f"{sign}${n / 1_000_000:.2f}M".replace(".00M", "M")
    if n >= 1_000:
        return f"{sign}${n / 1_000:.1f}k".replace(".0k", "k")
    return f"{sign}${n:.0f}" if n == n.to_integral_value() else f"{sign}${n:.2f}"


# --- dates -------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def short_date(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    return ts.strftime("%b %-d")


def iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts else None


# --- table cells -------------------------------------------------------------

PILL_CLASSES = ("draft", "sent", "scheduled", "paid", "unpaid", "overdue")
_PILL_RE = re.compile(r'^<span class="status-pill status-(draft|sent|scheduled|paid|unpaid|overdue)">([^<>]*)</span>$')
_LINK_RE = re.compile(r'^<a href="(https?://[^"<>\s]+)" target="_blank" rel="noopener">([^<>]*)</a>$')


def pill(text: str, cls: str) -> str:
    if cls not in PILL_CLASSES:
        cls = "draft"
    return f'<span class="status-pill status-{cls}">{html.escape(str(text))}</span>'


def link(href: str | None, label: str = "↗") -> str:
    if not href or not re.match(r"^https?://", href):
        return ""
    return f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener">{html.escape(label)}</a>'


def sanitize_cell(cell: Any) -> str:
    """Allow exactly one status pill or one link per cell; everything else is escaped text."""
    s = "" if cell is None else str(cell)
    if _PILL_RE.match(s) or _LINK_RE.match(s):
        return s
    return html.escape(s)


def sanitize_rows(rows: list[list[Any]]) -> list[list[str]]:
    return [[sanitize_cell(c) for c in row] for row in rows]


# --- status → pill class mapping -------------------------------------------


def status_pill(status: str | None) -> str:
    """Map an arbitrary status word onto the six allowed pill classes."""
    s = (status or "—").strip()
    key = s.lower().replace("_", " ")
    cls = "draft"
    if key in {"ready", "live", "done", "completed", "active", "paid", "cleared", "settled", "ok", "customer", "poc"}:
        cls = "paid"
    elif key in {"in progress", "started", "in flight", "sent", "engaged", "processed"}:
        cls = "sent"
    elif key in {"planned", "scheduled", "in review", "building", "queued", "advisory", "verify"}:
        cls = "scheduled"
    elif key in {"error", "failed", "hold", "overdue", "canceled", "cancelled"}:
        cls = "unpaid"
    elif key in {"unpaid", "awaiting payment", "open", "submitted"}:
        cls = "unpaid"
    return pill(s, cls)


# --- approvals ---------------------------------------------------------------


def row_to_approval(row: dict[str, Any]) -> Approval:
    """approvals row → Approval. An exec outside the allow-list is treated as record-only (exec=None)."""
    data = dict(row)
    exec_raw = data.get("exec")
    exec_obj: Exec | None = None
    if isinstance(exec_raw, dict):
        try:
            exec_obj = Exec.model_validate(exec_raw)
        except Exception:
            exec_obj = None
    data["exec"] = exec_obj
    return Approval.model_validate(data)


def fetch_approvals(
    conn: psycopg.Connection, tenant_id: str, module: str | None = None, status: str | None = "pending"
) -> list[Approval]:
    sql = "SELECT * FROM approvals WHERE tenant_id = %s"
    params: list[Any] = [tenant_id]
    if module:
        sql += " AND module = %s"
        params.append(module)
    if status:
        sql += " AND status = %s"
        params.append(status)
    sql += " ORDER BY created_at DESC, id"
    return [row_to_approval(r) for r in conn.execute(sql, params).fetchall()]


def connection_sync_at(conn: psycopg.Connection, tenant_id: str, sources: tuple[str, ...]) -> datetime:
    """max(last_sync_at) of the given sources, else now."""
    if sources:
        row = conn.execute(
            "SELECT max(last_sync_at) AS ts FROM connections WHERE tenant_id = %s AND source = ANY(%s)",
            (tenant_id, list(sources)),
        ).fetchone()
        if row and row["ts"]:
            return row["ts"]
    return now_utc()


def connected_sources(conn: psycopg.Connection, tenant_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT source, status, last_sync_at, config FROM connections WHERE tenant_id = %s", (tenant_id,)
    ).fetchall()
    return {r["source"]: r for r in rows}


def brain_slice(conn: psycopg.Connection, tenant_id: str, slice_name: str, limit: int = 300) -> str:
    row = conn.execute(
        "SELECT content FROM brain_docs WHERE tenant_id = %s AND slice = %s ORDER BY version DESC, updated_at DESC LIMIT 1",
        (tenant_id, slice_name),
    ).fetchone()
    if not row:
        return ""
    content = row["content"]
    if content.lstrip().startswith("---"):  # drop YAML front matter; it is metadata, not memory
        end = content.find("\n---", 3)
        content = content[end + 4 :] if end != -1 else content
    text = " ".join(content.split())
    return text[:limit]


def scalar(conn: psycopg.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    if not row:
        return None
    return next(iter(row.values()))
