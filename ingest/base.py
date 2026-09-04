"""Shared ingest plumbing: money parsing, timestamp parsing, and the diffing Upserter.

The Upserter writes typed rows and emits `events` (kind created|updated, diff {field: [old, new]}) only for
fields that actually changed. Running the same rows twice produces no new events — that is the idempotency
contract every source relies on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

FIXTURES_DIR_NAME = "fixtures"

_MONEY_RE = re.compile(r"^\s*(?P<sym>[$€£])?\s*(?P<num>-?[\d,]+(?:\.\d+)?)\s*(?P<ccy>[A-Za-z]{3})?\s*$")
_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}


def parse_money(value: Any, default_currency: str = "USD") -> tuple[Decimal, str]:
    """Return (amount, currency) from any Brex money shape.

    Accepts {value, currency, display}, {amount (cents), currency}, {quantity, instrument_code_string},
    strings like "517.26 USD" / "$8517.26 USD" / "12500.00", and bare numbers.
    """
    if value is None:
        return Decimal("0.00"), default_currency
    if isinstance(value, dict):
        ccy = value.get("currency") or value.get("instrument_code_string") or default_currency
        if "value" in value and value["value"] is not None:
            return _dec(value["value"]), str(ccy)
        if "quantity" in value and value["quantity"] is not None:
            return _dec(value["quantity"]), str(ccy)
        if "amount" in value and value["amount"] is not None:
            # Brex REST money: integer minor units
            amt = value["amount"]
            if isinstance(amt, int) and not isinstance(amt, bool):
                return (Decimal(amt) / 100).quantize(Decimal("0.01")), str(ccy)
            return _dec(amt), str(ccy)
        if "display" in value and value["display"]:
            return parse_money(value["display"], default_currency)
        return Decimal("0.00"), str(ccy)
    if isinstance(value, bool):
        raise ValueError("boolean is not a money value")
    if isinstance(value, int | float | Decimal):
        return _dec(value), default_currency
    if isinstance(value, str):
        m = _MONEY_RE.match(value)
        if not m:
            raise ValueError(f"unparseable money string: {value!r}")
        ccy = m.group("ccy")
        if ccy:
            ccy = ccy.upper()
        elif m.group("sym"):
            ccy = _SYMBOLS[m.group("sym")]
        else:
            ccy = default_currency
        return _dec(m.group("num").replace(",", "")), ccy
    raise ValueError(f"unsupported money value: {type(value).__name__}")


def _dec(v: Any) -> Decimal:
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount: {v!r}") from exc


def parse_ts(value: Any) -> datetime | None:
    """ISO strings (with Z), epoch seconds/millis (int/float/str) → aware UTC datetime. None stays None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float) and not isinstance(value, bool):
        secs = float(value)
        if secs > 1e11:  # milliseconds
            secs /= 1000.0
        return datetime.fromtimestamp(secs, tz=UTC)
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            return parse_ts(float(s))
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    raise ValueError(f"unparseable timestamp: {value!r}")


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    dt = parse_ts(value)
    return dt.date() if dt else None


def jsonable(v: Any) -> Any:
    """Make a value JSON-serializable for the events diff column."""
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, list | tuple):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    return v


def _norm(v: Any) -> Any:
    """Normalize for equality: Decimal→str at 2dp, datetimes→UTC isoformat, lists→list."""
    if isinstance(v, Decimal):
        return str(v.quantize(Decimal("0.01")))
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, tuple):
        return list(v)
    if isinstance(v, float):
        return str(Decimal(str(v)).quantize(Decimal("0.01")))
    return v


def diff_rows(old: dict[str, Any] | None, new: dict[str, Any], fields: list[str]) -> dict[str, list[Any]]:
    """{field: [old, new]} for every compared field that differs. old=None → every non-null field."""
    out: dict[str, list[Any]] = {}
    for f in fields:
        nv = new.get(f)
        ov = None if old is None else old.get(f)
        if _norm(ov) != _norm(nv):
            out[f] = [jsonable(ov), jsonable(nv)]
    return out


@dataclass
class UpsertResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged


@dataclass
class Upserter:
    """Diff-and-upsert rows into one typed table.

    table:       target table name
    source:      events.source (linear | slack | brex | vercel)
    key_cols:    business key columns besides tenant_id, e.g. ["id"] or ["source", "id"]
    columns:     all writable columns (tenant_id excluded); rows may omit some → NULL / default
    compare:     columns that participate in the diff (defaults to columns minus json_cols and skip_compare)
    json_cols:   columns stored as JSONB (wrapped with Jsonb)
    skip_compare: columns never diffed (raw, observed_at, ...)
    """

    table: str
    source: str
    key_cols: list[str]
    columns: list[str]
    json_cols: list[str] = field(default_factory=list)
    skip_compare: list[str] = field(default_factory=list)
    compare: list[str] | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z_]+", self.table):
            raise ValueError(f"bad table name: {self.table}")
        for c in self.columns + self.key_cols:
            if not re.fullmatch(r"[a-z0-9_]+", c):
                raise ValueError(f"bad column name: {c}")
        if self.compare is None:
            skip = set(self.json_cols) | set(self.skip_compare)
            self.compare = [c for c in self.columns if c not in skip]

    def _entity_id(self, row: dict[str, Any]) -> str:
        return ":".join(str(row[k]) for k in self.key_cols) if len(self.key_cols) > 1 else str(row[self.key_cols[0]])

    def _fetch_existing(self, conn: psycopg.Connection, tenant_id: str, rows: list[dict[str, Any]]) -> dict:
        if not rows:
            return {}
        cols = ", ".join(self.compare)
        keys = ", ".join(self.key_cols)
        existing: dict[tuple, dict[str, Any]] = {}
        key_tuples = [tuple(r[k] for k in self.key_cols) for r in rows]
        # One query per chunk of keys; small tables, so a simple IN-list is fine.
        chunk = 500
        for i in range(0, len(key_tuples), chunk):
            part = key_tuples[i : i + chunk]
            placeholders = ", ".join(["(" + ", ".join(["%s"] * len(self.key_cols)) + ")"] * len(part))
            params: list[Any] = [tenant_id]
            for kt in part:
                params.extend(kt)
            sql = f"SELECT {keys}, {cols} FROM {self.table} WHERE tenant_id = %s AND ({keys}) IN ({placeholders})"
            for found in conn.execute(sql, params).fetchall():
                existing[tuple(found[k] for k in self.key_cols)] = found
        return existing

    def upsert(self, conn: psycopg.Connection, tenant_id: str, rows: list[dict[str, Any]]) -> UpsertResult:
        res = UpsertResult()
        # Deterministic order + de-dup by key (last wins) so re-runs are byte-identical.
        by_key: dict[tuple, dict[str, Any]] = {}
        for r in rows:
            by_key[tuple(r[k] for k in self.key_cols)] = r
        ordered = [by_key[k] for k in sorted(by_key, key=lambda t: tuple(str(x) for x in t))]
        existing = self._fetch_existing(conn, tenant_id, ordered)

        all_cols = ["tenant_id", *self.columns]
        col_sql = ", ".join(all_cols)
        val_sql = ", ".join(["%s"] * len(all_cols))
        conflict = ", ".join(["tenant_id", *self.key_cols])
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in self.columns if c not in self.key_cols)
        sql = (
            f"INSERT INTO {self.table} ({col_sql}) VALUES ({val_sql}) ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        )

        for row in ordered:
            key = tuple(row[k] for k in self.key_cols)
            old = existing.get(key)
            changes = diff_rows(old, row, self.compare or [])
            if old is not None and not changes:
                res.unchanged += 1
                continue
            values: list[Any] = [tenant_id]
            for c in self.columns:
                v = row.get(c)
                if c in self.json_cols:
                    v = Jsonb(v if v is not None else {})
                values.append(v)
            conn.execute(sql, values)
            kind = "created" if old is None else "updated"
            event = {
                "source": self.source,
                "entity": self.table,
                "entity_id": self._entity_id(row),
                "kind": kind,
                "diff": changes,
            }
            conn.execute(
                "INSERT INTO events (tenant_id, source, entity, entity_id, kind, diff) VALUES (%s, %s, %s, %s, %s, %s)",
                (tenant_id, event["source"], event["entity"], event["entity_id"], kind, Jsonb(changes)),
            )
            res.events.append(event)
            if kind == "created":
                res.created += 1
            else:
                res.updated += 1
        return res


def dumps(obj: Any) -> str:
    return json.dumps(jsonable(obj), sort_keys=True, separators=(",", ":"))
