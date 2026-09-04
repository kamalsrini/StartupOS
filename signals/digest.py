"""Tier 0 evening digest — deterministic Markdown for Slack. No model call.

    python -m signals.digest [--post]

--post sends to settings.slack_digest_channel, and only when SLACK_BOT_TOKEN is present.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from common.settings import settings

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
MODULE_ORDER = ["cockpit", "sales", "finance", "build", "customers", "marketing", "web", "social", "security"]
TOP_N = 5


def gather(conn: psycopg.Connection, tenant_id: str, now: datetime) -> dict[str, Any]:
    open_signals = [
        dict(r)
        for r in conn.execute(
            """SELECT id, module, rule_id, severity, kind, title, meta, href, suggested_skill, first_seen_at
               FROM signals WHERE tenant_id = %s AND resolved_at IS NULL ORDER BY id""",
            (tenant_id,),
        ).fetchall()
    ]
    by_module: dict[str, int] = {}
    for s in open_signals:
        by_module[s["module"]] = by_module.get(s["module"], 0) + 1
    highs = sorted(
        (s for s in open_signals if s["severity"] == "high"),
        key=lambda s: (s["first_seen_at"] or now, s["id"]),
    )[:TOP_N]
    approvals_pending = conn.execute(
        "SELECT count(*) AS n FROM approvals WHERE tenant_id = %s AND status = 'pending'", (tenant_id,)
    ).fetchone()["n"]
    yesterday_start = now - timedelta(days=1)
    ev = conn.execute(
        """SELECT count(*) AS n, count(*) FILTER (WHERE kind = 'created') AS created
           FROM events WHERE tenant_id = %s AND occurred_at >= %s AND occurred_at < %s""",
        (tenant_id, yesterday_start, now),
    ).fetchone()
    resolved = conn.execute(
        "SELECT count(*) AS n FROM signals WHERE tenant_id = %s AND resolved_at >= %s AND resolved_at < %s",
        (tenant_id, yesterday_start, now),
    ).fetchone()["n"]
    return {
        "now": now,
        "open_total": len(open_signals),
        "by_module": by_module,
        "by_severity": {
            sev: sum(1 for s in open_signals if s["severity"] == sev) for sev in ("high", "medium", "low", "info")
        },
        "top_high": highs,
        "approvals_pending": approvals_pending,
        "events_yesterday": ev["n"],
        "events_created_yesterday": ev["created"],
        "signals_resolved_yesterday": resolved,
    }


def render(data: dict[str, Any]) -> str:
    now: datetime = data["now"]
    lines = [f"*StartupOS evening digest · {now.strftime('%a %b %-d, %Y')}*", ""]
    sev = data["by_severity"]
    lines.append(
        f"*Open signals:* {data['open_total']} ({sev['high']} high · {sev['medium']} medium · {sev['low']} low)"
    )
    if data["by_module"]:
        ordered = sorted(
            data["by_module"].items(),
            key=lambda kv: (MODULE_ORDER.index(kv[0]) if kv[0] in MODULE_ORDER else 99, kv[0]),
        )
        lines.append("  " + " · ".join(f"{m} {n}" for m, n in ordered))
    lines.append("")
    lines.append(f"*Top high signals ({len(data['top_high'])}):*")
    if data["top_high"]:
        for s in data["top_high"]:
            link = f" <{s['href']}|↗>" if s.get("href") else ""
            meta = f" — _{s['meta']}_" if s.get("meta") else ""
            lines.append(f"• [{s['module']}] {s['title']}{meta}{link}")
    else:
        lines.append("• none — nothing high is open")
    lines.append("")
    lines.append(f"*Approvals waiting:* {data['approvals_pending']}")
    lines.append(
        f"*Yesterday:* {data['events_yesterday']} changes ingested "
        f"({data['events_created_yesterday']} new) · {data['signals_resolved_yesterday']} signals resolved"
    )
    return "\n".join(lines)


def build(conn: psycopg.Connection, tenant_id: str, now: datetime | None = None) -> str:
    return render(gather(conn, tenant_id, now or datetime.now(UTC)))


def post_to_slack(text: str) -> bool:
    """Post the digest. Returns False (and does nothing) when the token or channel is missing."""
    token = settings.secret("env:SLACK_BOT_TOKEN")
    channel = settings.slack_digest_channel
    if not token or not channel:
        return False
    from slack_sdk import WebClient

    WebClient(token=token).chat_postMessage(channel=channel, text=text, mrkdwn=True)
    return True


def main(argv: list[str] | None = None) -> int:
    from common.db import ensure_tenant, get_conn

    parser = argparse.ArgumentParser(prog="signals.digest")
    parser.add_argument("--post", action="store_true", help="post to SLACK_DIGEST_CHANNEL (needs SLACK_BOT_TOKEN)")
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)
    with get_conn(args.dsn) as conn:
        ensure_tenant(conn, settings.tenant_id)
        text = build(conn, settings.tenant_id)
    print(text)
    if args.post:
        posted = post_to_slack(text)
        print("\n(posted to Slack)" if posted else "\n(not posted: SLACK_BOT_TOKEN or SLACK_DIGEST_CHANNEL missing)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
