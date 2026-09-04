"""Module snapshots: GET /modules/{name} assembled from Postgres tables only. Exactly 4 tiles, honest zeros."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from api.deps import (
    brain_slice,
    compact_money,
    connected_sources,
    connection_sync_at,
    fetch_approvals,
    get_db,
    get_tenant,
    link,
    now_utc,
    pill,
    sanitize_rows,
    scalar,
    short_date,
    status_pill,
)
from common.models import ModuleSnapshot, SignalView, Table, Tile

router = APIRouter(prefix="/modules", tags=["modules"])

NOT_CONNECTED = "not connected"
OPEN_ISSUE_SQL = "coalesce(status_type, '') NOT IN ('completed', 'canceled')"

# Static per-module descriptors (mirrors MODULE_SNAPSHOTS titles/crumbs/next from the Sep 3 artifact).
MODULES: dict[str, dict[str, Any]] = {
    "build": {
        "title": "Build",
        "crumb": "Engineering · Linear + Vercel",
        "sources": ("linear", "vercel"),
        "slice": "build",
        "next": [
            "GitHub MCP — PR/commit activity → engineer-week summaries",
            "PagerDuty / Sentry — incident → Linear action items",
        ],
    },
    "marketing": {
        "title": "Marketing",
        "crumb": "Content + attribution · Vercel + PostHog",
        "sources": ("vercel", "posthog", "apollo"),
        "slice": "gtm",
        "next": [
            "Ahrefs — SEO opportunity radar",
            "Klaviyo — nurture sequences",
            "Canva / Figma — brand-drift check on drafts",
        ],
    },
    "web": {
        "title": "Web",
        "crumb": "Website · Vercel",
        "sources": ("vercel", "posthog"),
        "slice": "build",
        "next": ["Cloudflare / DNS — domain + cert visibility", "Lighthouse — weekly perf + a11y score"],
    },
    "customers": {
        "title": "Customers",
        "crumb": "Accounts · Linear + Sales memory",
        "sources": ("linear", "apollo"),
        "slice": "customers",
        "next": ["Apollo accounts + deals", "HubSpot / Gmail — last-touch and reply history", "NPS + support themes"],
    },
    "social": {
        "title": "Social",
        "crumb": "LinkedIn · follow-up queue + posts",
        "sources": ("linkedin",),
        "slice": "gtm",
        "next": ["LinkedIn API — send + reply tracking", "X / Typefully — cross-post scheduler", "Mentions feed"],
    },
    "security": {
        "title": "IT & Security",
        "crumb": "Posture · Linear + Brex",
        "sources": ("linear", "brex", "github"),
        "slice": "decisions",
        "next": [
            "GitHub — Dependabot + secret-scanning alerts",
            "Okta / Google Workspace — MFA + offboarding evidence",
            "Vanta / Drata — SOC2 pipeline",
        ],
    },
    "commerce": {
        "title": "Commerce",
        "crumb": "Subscriptions + billing",
        "sources": ("stripe", "brex"),
        "slice": "pricing",
        "next": ["Stripe MCP", "Shopify MCP", "Chargebee MCP"],
    },
    "research": {
        "title": "Research",
        "crumb": "Customer + market research",
        "sources": ("gdrive",),
        "slice": "icp",
        "next": ["Fireflies / Gong — transcript ingest", "Dovetail-style theme synthesis"],
    },
    "sales": {
        "title": "Sales",
        "crumb": "Outbound · Apollo",
        "sources": ("apollo",),
        "slice": "gtm",
        "next": ["Apollo deals + conversations", "Gmail — reply threads"],
    },
    "cockpit": {
        "title": "Cockpit",
        "crumb": "Everything that needs you",
        "sources": ("linear", "slack", "brex", "vercel", "apollo"),
        "slice": "identity",
        "next": ["Slack digest channel", "Telegram gateway"],
    },
}

SOURCE_LABELS = {
    "linear": "Linear",
    "slack": "Slack",
    "brex": "Brex",
    "apollo": "Apollo",
    "vercel": "Vercel",
    "posthog": "PostHog",
    "github": "GitHub",
    "gdrive": "Google Drive",
    "gmail": "Gmail",
    "stripe": "Stripe",
    "linkedin": "LinkedIn",
}


def _source_line(name: str, connected: dict[str, dict[str, Any]]) -> str:
    parts = []
    for s in MODULES[name]["sources"]:
        label = SOURCE_LABELS.get(s, s)
        parts.append(label if s in connected else f"{label} ({NOT_CONNECTED})")
    return " · ".join(parts) if parts else "Not connected"


def _nc_tile(label: str, source: str) -> Tile:
    return Tile(label=label, val="—", sub=f"{SOURCE_LABELS.get(source, source)} {NOT_CONNECTED}")


# --- per-module builders -----------------------------------------------------


def _build(conn: psycopg.Connection, t: str, connected: dict) -> tuple[list[Tile], Table | None, Table | None]:
    open_n = scalar(conn, f"SELECT count(*) FROM issues WHERE tenant_id=%s AND {OPEN_ISSUE_SQL}", (t,))
    total_n = scalar(conn, "SELECT count(*) FROM issues WHERE tenant_id=%s", (t,))
    hi = conn.execute(
        f"""SELECT count(*) FILTER (WHERE priority=1) AS urgent, count(*) FILTER (WHERE priority=2) AS high,
                   count(*) FILTER (WHERE assignee IS NULL) AS unassigned
              FROM issues WHERE tenant_id=%s AND priority IN (1,2) AND {OPEN_ISSUE_SQL}""",
        (t,),
    ).fetchone()
    hi_total = hi["urgent"] + hi["high"]
    prog = conn.execute(
        "SELECT id, assignee FROM issues WHERE tenant_id=%s AND status_type='started' ORDER BY updated_at DESC NULLS LAST",
        (t,),
    ).fetchall()
    prog_sub = ", ".join(r["id"] for r in prog[:3]) if prog else "nothing in progress"
    if len(prog) > 3:
        prog_sub += f" +{len(prog) - 3}"

    # Latest production deployment per project
    deploys = conn.execute(
        """SELECT DISTINCT ON (project) project, state, created_at, commit_message, url
             FROM deployments WHERE tenant_id=%s AND coalesce(target,'production')='production'
            ORDER BY project, created_at DESC NULLS LAST""",
        (t,),
    ).fetchall()
    ready = sum(1 for d in deploys if d["state"] == "READY")
    if "vercel" in connected or deploys:
        deploy_tile = Tile(
            label="Prod deploys",
            val=f"{ready} / {len(deploys)}",
            sub=" · ".join(d["project"] for d in deploys[:3]) if deploys else "no deployments ingested",
            cls="bad" if any(d["state"] == "ERROR" for d in deploys) else "",
        )
    else:
        deploy_tile = _nc_tile("Prod deploys", "vercel")

    tiles = [
        Tile(label="Open issues", val=str(open_n), sub=f"of {total_n} ingested" if total_n else "no issues ingested"),
        Tile(
            label="Urgent / High",
            val=str(hi_total),
            sub=f"{hi['urgent']} urgent · {hi['high']} high · {hi['unassigned']} unassigned",
            cls="warn" if hi["unassigned"] else "",
        ),
        Tile(label="In progress", val=str(len(prog)), sub=prog_sub),
        deploy_tile,
    ]

    projects = conn.execute(
        f"""SELECT p.name, p.status, p.target_date, p.lead, p.url, p.updated_at,
                   (SELECT count(*) FROM issues i WHERE i.tenant_id=p.tenant_id AND i.project=p.name AND {OPEN_ISSUE_SQL}) AS open_n
              FROM projects p WHERE p.tenant_id=%s ORDER BY p.updated_at DESC NULLS LAST, p.name""",
        (t,),
    ).fetchall()
    table = Table(
        title="Projects",
        accent="Linear · auto-synced",
        cols=["Project", "Status", "Open", "Updated", ""],
        rows=sanitize_rows(
            [
                [
                    p["name"],
                    status_pill(p["status"]),
                    str(p["open_n"]) if p["open_n"] else "—",
                    short_date(p["updated_at"]) + (f" · lead {p['lead']}" if p["lead"] else ""),
                    link(p["url"]),
                ]
                for p in projects
            ]
        ),
    )
    table2 = Table(
        title="Deployments",
        accent="Vercel · production",
        cols=["Project", "State", "Last deploy", "Commit", ""],
        rows=sanitize_rows(
            [
                [
                    d["project"],
                    status_pill(d["state"]),
                    short_date(d["created_at"]),
                    d["commit_message"] or "",
                    link(d["url"]),
                ]
                for d in deploys
            ]
        ),
    )
    return tiles, table, table2


def _customers(conn: psycopg.Connection, t: str, connected: dict) -> tuple[list[Tile], Table | None, Table | None]:
    accounts = conn.execute(
        "SELECT * FROM accounts WHERE tenant_id=%s ORDER BY (stage='poc') DESC, last_touch_at DESC NULLS LAST, name",
        (t,),
    ).fetchall()
    poc = [a for a in accounts if a["stage"] == "poc"]
    engaged = [a for a in accounts if a["stage"] in ("engaged", "poc", "advisory", "customer")]
    reach = conn.execute(
        "SELECT coalesce(sum(contacts),0) AS c, count(*) AS n FROM sequences WHERE tenant_id=%s", (t,)
    ).fetchone()
    # Open asks: open issues carrying an account label (account id or name).
    labels = [a["id"] for a in accounts] + [a["name"] for a in accounts]
    asks = conn.execute(
        f"""SELECT id, title FROM issues WHERE tenant_id=%s AND {OPEN_ISSUE_SQL}
              AND (labels && %s::text[] OR 'customer' = ANY(labels) OR 'Customer' = ANY(labels))
            ORDER BY updated_at DESC NULLS LAST""",
        (t, labels),
    ).fetchall()
    reach_tile = (
        Tile(label="Outbound reach", val=str(reach["c"]), sub=f"contacts · {reach['n']} sequences")
        if ("apollo" in connected or reach["n"])
        else _nc_tile("Outbound reach", "apollo")
    )
    tiles = [
        Tile(label="Active POC", val=str(len(poc)), sub=" · ".join(a["name"] for a in poc[:3]) or "no POC accounts"),
        Tile(
            label="Engaged accounts",
            val=str(len(engaged)),
            sub=" · ".join(a["name"] for a in engaged[:3]) or "no engaged accounts",
        ),
        reach_tile,
        Tile(
            label="Open asks",
            val=str(len(asks)),
            sub=" · ".join(a["id"] for a in asks[:3]) or "no account-labelled issues open",
            cls="warn" if asks else "",
        ),
    ]
    table = Table(
        title="Accounts",
        accent="Account 360 · v0",
        cols=["Account", "Stage", "Champion", "Last touch", "Notes"],
        rows=sanitize_rows(
            [
                [
                    a["name"],
                    status_pill(
                        (a["stage"] or "—").upper() if a["stage"] in ("poc", "hold") else (a["stage"] or "—").title()
                    ),
                    a["champion"] or "—",
                    short_date(a["last_touch_at"]) + (f" · ICP {a['icp_score']}" if a["icp_score"] is not None else ""),
                    (a["notes"] or "")[:120],
                ]
                for a in accounts
            ]
        ),
    )
    return tiles, table, None


def _sales(conn: psycopg.Connection, t: str, connected: dict) -> tuple[list[Tile], Table | None, Table | None]:
    seqs = conn.execute("SELECT * FROM sequences WHERE tenant_id=%s ORDER BY observed_at DESC, name", (t,)).fetchall()
    if not seqs and "apollo" not in connected:
        tiles = [
            _nc_tile("Active sequences", "apollo"),
            _nc_tile("Contacts", "apollo"),
            _nc_tile("Replies", "apollo"),
            _nc_tile("Reply rate", "apollo"),
        ]
    else:
        active = [s for s in seqs if (s["status"] or "").lower() == "active"]
        contacts = sum(s["contacts"] or 0 for s in seqs)
        delivered = sum(s["delivered"] or 0 for s in seqs)
        replied = sum(s["replied"] or 0 for s in seqs)
        rate = f"{(replied / delivered * 100):.1f}%" if delivered else "—"
        tiles = [
            Tile(label="Active sequences", val=str(len(active)), sub=f"of {len(seqs)} sequences"),
            Tile(label="Contacts", val=str(contacts), sub=f"{delivered} delivered"),
            Tile(label="Replies", val=str(replied), sub="across all sequences", cls="warn" if replied else ""),
            Tile(label="Reply rate", val=rate, sub="replied / delivered"),
        ]
    table = Table(
        title="Sequences",
        accent="Apollo",
        cols=["Sequence", "Status", "Contacts", "Delivered", "Opened", "Replied", "Bounced"],
        rows=sanitize_rows(
            [
                [
                    s["name"],
                    status_pill(s["status"]),
                    str(s["contacts"] or 0),
                    str(s["delivered"] or 0),
                    str(s["opened"] or 0),
                    str(s["replied"] or 0),
                    str(s["bounced"] or 0),
                ]
                for s in seqs
            ]
        ),
    )
    return tiles, table, None


def _web(conn: psycopg.Connection, t: str, connected: dict) -> tuple[list[Tile], Table | None, Table | None]:
    since = now_utc() - timedelta(days=90)
    latest = conn.execute(
        """SELECT DISTINCT ON (project) project, state, created_at, url, commit_message FROM deployments
            WHERE tenant_id=%s ORDER BY project, created_at DESC NULLS LAST""",
        (t,),
    ).fetchall()
    prod = conn.execute(
        """SELECT project, state, created_at FROM deployments WHERE tenant_id=%s AND coalesce(target,'production')='production'
            ORDER BY created_at DESC NULLS LAST LIMIT 1""",
        (t,),
    ).fetchone()
    d90 = conn.execute(
        """SELECT count(*) AS n, count(*) FILTER (WHERE state='READY') AS ready, count(*) FILTER (WHERE state='ERROR') AS err
             FROM deployments WHERE tenant_id=%s AND created_at >= %s""",
        (t, since),
    ).fetchone()
    if not latest and "vercel" not in connected:
        tiles = [
            _nc_tile("Production", "vercel"),
            _nc_tile("Deploys (90d)", "vercel"),
            _nc_tile("Projects", "vercel"),
            _nc_tile("Web analytics", "posthog"),
        ]
    else:
        tiles = [
            Tile(
                label="Production",
                val=prod["state"] if prod else "—",
                sub=f"{prod['project']} · deployed {short_date(prod['created_at'])}"
                if prod
                else "no production deploy ingested",
                cls="bad" if prod and prod["state"] == "ERROR" else "",
            ),
            Tile(
                label="Deploys (90d)",
                val=str(d90["n"]),
                sub=f"{d90['ready']} READY · {d90['err']} ERROR",
                cls="warn" if d90["err"] else "",
            ),
            Tile(label="Projects", val=str(len(latest)), sub="with at least one deployment"),
            Tile(
                label="Web analytics",
                val="On" if "posthog" in connected else "Off",
                sub="PostHog connected" if "posthog" in connected else f"PostHog {NOT_CONNECTED}",
                cls="" if "posthog" in connected else "bad",
            ),
        ]
    table = Table(
        title="Vercel projects",
        accent="latest deployment per project",
        cols=["Project", "State", "Last activity", "Commit", ""],
        rows=sanitize_rows(
            [
                [
                    d["project"],
                    status_pill(d["state"]),
                    short_date(d["created_at"]),
                    d["commit_message"] or "",
                    link(d["url"]),
                ]
                for d in latest
            ]
        ),
    )
    return tiles, table, None


def _marketing(conn: psycopg.Connection, t: str, connected: dict) -> tuple[list[Tile], Table | None, Table | None]:
    docs = conn.execute(
        "SELECT title, url, updated_at FROM documents WHERE tenant_id=%s AND source='web' ORDER BY updated_at DESC LIMIT 25",
        (t,),
    ).fetchall()
    drafts = scalar(
        conn,
        "SELECT count(*) FROM signals WHERE tenant_id=%s AND rule_id='sales.stale_draft_campaign' AND resolved_at IS NULL",
        (t,),
    )
    tiles = [
        Tile(
            label="Content assets",
            val=str(len(docs)),
            sub="web documents ingested" if docs else "no web documents ingested",
        ),
        Tile(
            label="Campaigns in draft",
            val=str(drafts),
            sub="stale drafts flagged by signals" if drafts else "none flagged",
            cls="warn" if drafts else "",
        ),
        Tile(
            label="Attribution",
            val="On" if "posthog" in connected else "Off",
            sub="PostHog" if "posthog" in connected else f"PostHog {NOT_CONNECTED}",
            cls="" if "posthog" in connected else "warn",
        ),
        Tile(
            label="Web analytics",
            val="On" if "vercel" in connected else "Off",
            sub="Vercel" if "vercel" in connected else f"Vercel {NOT_CONNECTED}",
            cls="" if "vercel" in connected else "bad",
        ),
    ]
    table = Table(
        title="Content assets",
        accent="documents · source=web",
        cols=["Asset", "Updated", ""],
        rows=sanitize_rows([[d["title"] or "(untitled)", short_date(d["updated_at"]), link(d["url"])] for d in docs]),
    )
    return tiles, table, None


def _social(conn: psycopg.Connection, t: str, connected: dict) -> tuple[list[Tile], Table | None, Table | None]:
    backlog = scalar(
        conn,
        "SELECT count(*) FROM signals WHERE tenant_id=%s AND rule_id='social.queue_backlog' AND resolved_at IS NULL",
        (t,),
    )
    li = connected.get("linkedin")
    tiles = [
        _nc_tile("DM queue", "linkedin") if not li else Tile(label="DM queue", val="—", sub="queue size not ingested"),
        _nc_tile("Posts drafted", "linkedin"),
        Tile(
            label="Queue backlog signals",
            val=str(backlog),
            sub="open social.queue_backlog",
            cls="warn" if backlog else "",
        ),
        Tile(
            label="Queue refresh",
            val=short_date(li["last_sync_at"]) if li and li["last_sync_at"] else "—",
            sub="last sync" if li else f"LinkedIn {NOT_CONNECTED}",
        ),
    ]
    return tiles, None, None


def _security(conn: psycopg.Connection, t: str, connected: dict) -> tuple[list[Tile], Table | None, Table | None]:
    sec_issues = conn.execute(
        f"""SELECT id, title, assignee FROM issues WHERE tenant_id=%s AND {OPEN_ISSUE_SQL}
              AND EXISTS (SELECT 1 FROM unnest(labels) l WHERE lower(l) IN ('security', 'vulnerability'))
            ORDER BY updated_at DESC NULLS LAST""",
        (t,),
    ).fetchall()
    dupes = scalar(
        conn,
        "SELECT count(*) FROM signals WHERE tenant_id=%s AND rule_id='build.duplicate_titles' AND resolved_at IS NULL",
        (t,),
    )
    admins = scalar(conn, "SELECT count(*) FROM users WHERE tenant_id=%s AND role IN ('owner','admin')", (t,))
    cards = conn.execute(
        "SELECT count(*) AS n, count(*) FILTER (WHERE status='ACTIVE') AS active FROM cards WHERE tenant_id=%s", (t,)
    ).fetchone()
    bills = conn.execute(
        "SELECT count(*) AS n, count(*) FILTER (WHERE status='APPROVED') AS approved FROM bills WHERE tenant_id=%s",
        (t,),
    ).fetchone()
    bad_refs = scalar(
        conn,
        "SELECT count(*) FROM connections WHERE tenant_id=%s AND secret_ref NOT LIKE 'env:%%' AND secret_ref NOT LIKE 'kv:%%'",
        (t,),
    )
    n_conn = scalar(conn, "SELECT count(*) FROM connections WHERE tenant_id=%s", (t,))
    tiles = [
        Tile(
            label="Security issues",
            val=str(len(sec_issues)),
            sub=" · ".join(i["id"] for i in sec_issues[:3]) or "no open security-labelled issues",
        ),
        Tile(
            label="Duplicate issues",
            val=str(dupes),
            sub="open build.duplicate_titles signals",
            cls="warn" if dupes else "",
        ),
        Tile(
            label="Admin accounts",
            val=str(admins),
            sub=f"StartupOS owners/admins · {cards['active']} of {cards['n']} cards active",
        ),
        Tile(label="SOC2", val="Not started", sub="no evidence pipeline connected", cls="warn"),
    ]
    rows = [
        [
            "Secrets management",
            pill("OK", "paid") if not bad_refs else pill("Fix", "unpaid"),
            f"{n_conn} connections · all secret_ref env:/kv:"
            if not bad_refs
            else f"{bad_refs} connections with raw refs",
            "—",
            "",
        ],
        [
            "Card + spend limits",
            pill("OK", "paid") if cards["n"] else pill("Verify", "scheduled"),
            f"Brex: {cards['active']} active of {cards['n']} cards" if cards["n"] else "no cards ingested",
            "—",
            "",
        ],
        [
            "Bill pay approval",
            pill("OK", "paid") if bills["n"] else pill("Verify", "scheduled"),
            f"{bills['approved']} of {bills['n']} bills APPROVED in Brex · none via StartupOS"
            if bills["n"]
            else "no bills ingested",
            "—",
            "",
        ],
        ["MFA on admin accounts", pill("Verify", "scheduled"), "not recorded", "—", ""],
        [
            "Repo visibility",
            pill("OK", "paid") if "github" in connected else pill("Verify", "scheduled"),
            "GitHub connected" if "github" in connected else f"GitHub {NOT_CONNECTED}",
            "—",
            "",
        ],
        ["SOC2 evidence", pill("Not started", "draft"), "—", "—", ""],
    ]
    table = Table(
        title="Controls",
        accent="v0 posture · derived from tables",
        cols=["Control", "Status", "Evidence", "Owner", ""],
        rows=sanitize_rows(rows),
    )
    return tiles, table, None


def _commerce(conn: psycopg.Connection, t: str, connected: dict) -> tuple[list[Tile], Table | None, Table | None]:
    last_in = conn.execute(
        "SELECT amount, occurred_at, counterparty FROM transactions WHERE tenant_id=%s AND amount > 0 ORDER BY occurred_at DESC LIMIT 1",
        (t,),
    ).fetchone()
    providers = [s for s in ("stripe", "shopify", "chargebee") if s in connected]
    tiles = [
        Tile(
            label="MRR",
            val="—" if "stripe" not in connected else "$0",
            sub=f"Stripe {NOT_CONNECTED}" if "stripe" not in connected else "no subscriptions ingested",
        ),
        Tile(
            label="Subscriptions",
            val="—" if "stripe" not in connected else "0",
            sub=f"Stripe {NOT_CONNECTED}" if "stripe" not in connected else "Stripe",
        ),
        Tile(
            label="Incoming funds (Brex)",
            val=compact_money(last_in["amount"]) if last_in else "$0",
            sub=f"{short_date(last_in['occurred_at'])} · last inflow · see Finance"
            if last_in
            else "no inflows ingested",
        ),
        Tile(label="Providers", val=f"{len(providers)} / 3", sub="Stripe · Shopify · Chargebee"),
    ]
    return tiles, None, None


def _research(conn: psycopg.Connection, t: str, connected: dict) -> tuple[list[Tile], Table | None, Table | None]:
    docs = conn.execute(
        "SELECT source, count(*) AS n FROM documents WHERE tenant_id=%s GROUP BY source", (t,)
    ).fetchall()
    by_src = {d["source"]: d["n"] for d in docs}
    scored = scalar(conn, "SELECT count(*) FROM accounts WHERE tenant_id=%s AND icp_score IS NOT NULL", (t,))
    tiles = [
        Tile(label="Interviews", val="0", sub="no transcripts ingested"),
        Tile(label="Account briefs", val=str(scored), sub="accounts with an ICP score"),
        Tile(
            label="Documents",
            val=str(sum(by_src.values())),
            sub=" · ".join(f"{k} {v}" for k, v in sorted(by_src.items())) or f"Drive {NOT_CONNECTED}",
        ),
        Tile(label="Open questions", val="—", sub="from decisions.md (not tracked yet)"),
    ]
    return tiles, None, None


def _cockpit(conn: psycopg.Connection, t: str, connected: dict) -> tuple[list[Tile], Table | None, Table | None]:
    open_sig = conn.execute(
        "SELECT count(*) AS n, count(*) FILTER (WHERE severity='high') AS high FROM signals WHERE tenant_id=%s AND resolved_at IS NULL",
        (t,),
    ).fetchone()
    pending = scalar(conn, "SELECT count(*) FROM approvals WHERE tenant_id=%s AND status='pending'", (t,))
    runs = conn.execute(
        """SELECT count(*) AS n, coalesce(sum(cost_usd),0) AS cost FROM runs
            WHERE tenant_id=%s AND started_at >= date_trunc('month', now())""",
        (t,),
    ).fetchone()
    tiles = [
        Tile(
            label="Open signals",
            val=str(open_sig["n"]),
            sub=f"{open_sig['high']} high",
            cls="warn" if open_sig["high"] else "",
        ),
        Tile(label="Pending approvals", val=str(pending), sub="waiting for you", cls="warn" if pending else ""),
        Tile(label="Runs MTD", val=str(runs["n"]), sub="agent invocations this month"),
        Tile(label="Spend MTD", val=f"${runs['cost']:.2f}", sub="model cost this month"),
    ]
    return tiles, None, None


BUILDERS = {
    "build": _build,
    "customers": _customers,
    "sales": _sales,
    "web": _web,
    "marketing": _marketing,
    "social": _social,
    "security": _security,
    "commerce": _commerce,
    "research": _research,
    "cockpit": _cockpit,
}

SIGNAL_ACTIONS = {
    "build.unassigned_high": "Assign",
    "build.stale_in_progress": "Nudge",
    "build.duplicate_titles": "Merge",
    "build.deploy_failed": "Log",
    "finance.bill_due_7d": "Pay in Brex",
    "finance.cash_low": "Review",
    "finance.unmatched_inflow": "Match",
    "customers.ask_untouched": "Prioritize",
    "sales.reply_detected": "Draft reply",
    "sales.stale_draft_campaign": "Review",
    "marketing.analytics_off": "Enable",
    "social.queue_backlog": "Send next 5",
}


def module_signals(conn: psycopg.Connection, tenant_id: str, module: str) -> list[SignalView]:
    rows = conn.execute(
        """SELECT s.*, (SELECT a.id FROM approvals a WHERE a.signal_id = s.id ORDER BY (a.status='pending') DESC, a.created_at DESC LIMIT 1) AS approval_id
             FROM signals s WHERE s.tenant_id=%s AND s.module=%s AND s.resolved_at IS NULL
            ORDER BY CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 WHEN 'low' THEN 2 ELSE 3 END, last_seen_at DESC""",
        (tenant_id, module),
    ).fetchall()
    out = []
    for r in rows:
        kind = r["kind"] if r["kind"] in ("reply", "click", "open", "bounce") else "open"
        action = SIGNAL_ACTIONS.get(r["rule_id"], "Open" if r["href"] else "")
        out.append(
            SignalView(
                id=r["id"],
                kind=kind,
                title=r["title"],
                meta=r["meta"] or f"{r['severity']} · {r['rule_id']}",
                action=action,
                href=r["href"],
                approval_id=r["approval_id"],
            )
        )
    return out


def build_snapshot(conn: psycopg.Connection, tenant_id: str, name: str) -> ModuleSnapshot:
    if name not in MODULES:
        raise HTTPException(status_code=404, detail=f"unknown module {name!r}")
    spec = MODULES[name]
    connected = connected_sources(conn, tenant_id)
    tiles, table, table2 = BUILDERS[name](conn, tenant_id, connected)
    assert len(tiles) == 4
    return ModuleSnapshot(
        name=name,
        title=spec["title"],
        crumb=spec["crumb"],
        source=_source_line(name, connected),
        snapshot_at=connection_sync_at(conn, tenant_id, spec["sources"]),
        live=False,
        memory=brain_slice(conn, tenant_id, spec["slice"]),
        tiles=tiles,
        signals=module_signals(conn, tenant_id, name),
        table=table if table and table.rows else (table if table and name in ("build", "customers") else None),
        table2=table2 if table2 and table2.rows else None,
        approvals=fetch_approvals(conn, tenant_id, module=name, status="pending"),
        next=spec["next"],
    )


@router.get("/{name}", response_model=ModuleSnapshot)
def get_module(
    name: str, conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)
) -> ModuleSnapshot:
    return build_snapshot(conn, tenant_id, name)
