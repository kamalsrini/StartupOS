"""Signal engine: load ctx from Postgres → evaluate rules → upsert `signals` idempotently.

    python -m signals.engine [--now 2026-09-04T12:00:00Z]

Signals whose rule no longer matches get `resolved_at = now`; a signal that reappears is re-opened
(`resolved_at` cleared, `last_seen_at` bumped, `first_seen_at` kept).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from brain.sync import parse_front_matter
from signals.rules import RULES, evaluate_all

SEQUENCE_EVENT_WINDOW = timedelta(hours=24)


def _rows(conn: psycopg.Connection, sql: str, params: tuple) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def load_ctx(conn: psycopg.Connection, tenant_id: str, now: datetime) -> dict[str, Any]:
    """Everything the rules need, as plain dicts. One query per table."""
    gtm_row = conn.execute(
        "SELECT content FROM brain_docs WHERE tenant_id = %s AND slice = 'gtm' ORDER BY path LIMIT 1", (tenant_id,)
    ).fetchone()
    gtm_meta = parse_front_matter(gtm_row["content"])[0] if gtm_row else {}
    social = {"linkedin_pending": int(gtm_meta.get("linkedin_queue_pending") or 0)}
    return build_ctx(
        issues=_rows(conn, "SELECT * FROM issues WHERE tenant_id = %s ORDER BY id", (tenant_id,)),
        bills=_rows(conn, "SELECT * FROM bills WHERE tenant_id = %s ORDER BY id", (tenant_id,)),
        accounts_bank=_rows(conn, "SELECT * FROM accounts_bank WHERE tenant_id = %s ORDER BY id", (tenant_id,)),
        transactions=_rows(conn, "SELECT * FROM transactions WHERE tenant_id = %s ORDER BY id", (tenant_id,)),
        deployments=_rows(conn, "SELECT * FROM deployments WHERE tenant_id = %s ORDER BY id", (tenant_id,)),
        sequences=_rows(conn, "SELECT * FROM sequences WHERE tenant_id = %s ORDER BY id", (tenant_id,)),
        sequence_events=_rows(
            conn,
            """SELECT id, entity_id, kind, diff, occurred_at FROM events
               WHERE tenant_id = %s AND entity = 'sequences' AND occurred_at >= %s ORDER BY id""",
            (tenant_id, now - SEQUENCE_EVENT_WINDOW),
        ),
        connections=_rows(
            conn, "SELECT source, status, config FROM connections WHERE tenant_id = %s ORDER BY source", (tenant_id,)
        ),
        accounts=_rows(conn, "SELECT id, name, stage FROM accounts WHERE tenant_id = %s ORDER BY id", (tenant_id,)),
        gtm=gtm_meta,
        social=social,
    )


def build_ctx(
    *,
    issues: list[dict[str, Any]] | None = None,
    bills: list[dict[str, Any]] | None = None,
    accounts_bank: list[dict[str, Any]] | None = None,
    transactions: list[dict[str, Any]] | None = None,
    deployments: list[dict[str, Any]] | None = None,
    sequences: list[dict[str, Any]] | None = None,
    sequence_events: list[dict[str, Any]] | None = None,
    connections: list[dict[str, Any]] | None = None,
    accounts: list[dict[str, Any]] | None = None,
    invoices: list[dict[str, Any]] | None = None,
    gtm: dict[str, Any] | None = None,
    social: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a rule ctx from row lists. Shared by the DB loader and by tests that skip Postgres."""
    return {
        "issues": issues or [],
        "bills": bills or [],
        "accounts_bank": accounts_bank or [],
        "transactions": transactions or [],
        "deployments": deployments or [],
        "sequences": sequences or [],
        "sequence_events": sequence_events or [],
        "connections": connections or [],
        "accounts": accounts or [],
        "invoices": invoices or [],
        "gtm": gtm or {},
        "social": social or {"linkedin_pending": 0},
        "config": config or {"cash_floor": None},
    }


def evaluate(ctx: dict[str, Any], now: datetime | None = None) -> list[dict[str, Any]]:
    """Pure: ctx → sorted list of signal dicts. `now` defaults to the wall clock."""
    return evaluate_all(ctx, now or datetime.now(UTC))


def upsert_signals(
    conn: psycopg.Connection, tenant_id: str, signals: list[dict[str, Any]], now: datetime
) -> dict[str, int]:
    """Write signals; resolve open ones that no longer fire. Returns counts."""
    ids = [s["id"] for s in signals]
    for s in signals:
        conn.execute(
            """INSERT INTO signals (id, tenant_id, module, rule_id, severity, kind, title, meta, entity, entity_id,
                                    suggested_skill, href, first_seen_at, last_seen_at, resolved_at)
               VALUES (%(id)s, %(tenant_id)s, %(module)s, %(rule_id)s, %(severity)s, %(kind)s, %(title)s, %(meta)s,
                       %(entity)s, %(entity_id)s, %(suggested_skill)s, %(href)s, %(now)s, %(now)s, NULL)
               ON CONFLICT (tenant_id, id) DO UPDATE SET
                 module = EXCLUDED.module, severity = EXCLUDED.severity, kind = EXCLUDED.kind,
                 title = EXCLUDED.title, meta = EXCLUDED.meta, entity = EXCLUDED.entity,
                 entity_id = EXCLUDED.entity_id, suggested_skill = EXCLUDED.suggested_skill, href = EXCLUDED.href,
                 last_seen_at = EXCLUDED.last_seen_at, resolved_at = NULL""",
            {**s, "tenant_id": tenant_id, "now": now},
        )
    rule_ids = [r.id for r in RULES]
    resolved = conn.execute(
        """UPDATE signals SET resolved_at = %s
           WHERE tenant_id = %s AND resolved_at IS NULL AND rule_id = ANY(%s) AND NOT (id = ANY(%s))""",
        (now, tenant_id, rule_ids, ids),
    ).rowcount
    return {"fired": len(signals), "resolved": resolved}


def run(conn: psycopg.Connection, tenant_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    ctx = load_ctx(conn, tenant_id, now)
    signals = evaluate(ctx, now)
    counts = upsert_signals(conn, tenant_id, signals, now)
    return {"now": now, "signals": signals, **counts}


def main(argv: list[str] | None = None) -> int:
    from common.db import ensure_tenant, get_conn
    from common.settings import settings

    parser = argparse.ArgumentParser(prog="signals.engine")
    parser.add_argument("--now", default=None, help="ISO timestamp to evaluate against (default: wall clock)")
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    now = None
    if args.now:
        s = args.now[:-1] + "+00:00" if args.now.endswith("Z") else args.now
        now = datetime.fromisoformat(s)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
    with get_conn(args.dsn) as conn:
        ensure_tenant(conn, settings.tenant_id)
        result = run(conn, settings.tenant_id, now)
    print(f"signals: {result['fired']} fired, {result['resolved']} resolved")
    for s in result["signals"]:
        print(f"  [{s['severity']:6}] {s['id']} — {s['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
