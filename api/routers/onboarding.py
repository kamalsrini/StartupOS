"""Onboarding (Day 0): tenant → connections → compile → confirm cards → cadence → status. Deterministic, no model."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from api.deps import api_dsn, current_principal, get_db, get_tenant, now_utc, scalar, set_session_cookie
from auth import bootstrap, google, sessions
from auth.identity import Principal
from common.db import get_conn
from common.ids import run_id
from common.models import MemoryCard

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

SOURCES = ("linear", "slack", "brex", "apollo", "vercel", "posthog", "github", "gdrive", "gmail", "stripe")
SLICES: tuple[str, ...] = ("identity", "icp", "voice", "pricing", "team")
DRAFT_MARK = "Draft — confirm or edit."
COMPILE_SKILL = "onboarding.compile"

# Which two sources we recommend, by what the website suggests (deterministic keyword match; brief §5.1 step 2).
RECOMMENDATIONS: list[tuple[tuple[str, ...], tuple[str, str]]] = [
    (("dev", "api", "sdk", "security", "platform", "infra", "software", "ai", "code"), ("linear", "slack")),
    (("agency", "consult", "studio", "services", "design", "law", "account"), ("brex", "gmail")),
    (("shop", "store", "commerce", "retail", "brand"), ("stripe", "slack")),
]


class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    website: str | None = None
    email: str | None = None
    timezone: str | None = None
    name_of_owner: str | None = Field(default=None, max_length=120)
    bootstrap_token: str | None = None  # STARTUPOS_BOOTSTRAP_TOKEN — single-operator install
    google_id_token: str | None = None  # verified Google OIDC id_token — self-serve sign-up


class ConnectionIn(BaseModel):
    source: str
    secret_ref: str
    config: dict[str, Any] = {}


class ConfirmIn(BaseModel):
    slice: Literal["identity", "icp", "voice", "pricing", "team"]
    content: str = Field(min_length=1)


class CadenceIn(BaseModel):
    timezone: str = "America/Los_Angeles"
    pulse_hour: int = Field(default=7, ge=0, le=23)
    channel: Literal["web", "slack", "both"] = "web"
    tier2_tokens_allowed: int = Field(default=1_500_000, ge=0)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:40] or "tenant"


def _normalize_site(website: str | None) -> str | None:
    if not website:
        return None
    w = website.strip()
    if not re.match(r"^https?://", w):
        w = "https://" + w
    return w


def template_slices(name: str, website: str | None) -> dict[str, str]:
    site = website or "(website not provided)"
    return {
        "identity": f"# Identity\n\n{name} — {site}.\n\nMission: (one sentence about what {name} does and for whom).\n\n{DRAFT_MARK}",
        "icp": f"# Ideal customer profile\n\nWho buys from {name}: (role, company type, size, trigger).\n\nSource to confirm: {site}\n\n{DRAFT_MARK}",
        "voice": f"# Brand voice\n\nTone for {name}: (e.g. technical, evidence-first, no hype).\n\nSource to confirm: {site}\n\n{DRAFT_MARK}",
        "pricing": f"# Pricing & offer\n\nPlans and prices for {name}: (list tiers).\n\nSource to confirm: {site}/pricing\n\n{DRAFT_MARK}",
        "team": f"# Team\n\nPeople and areas of responsibility at {name}: (name — AOR).\n\n{DRAFT_MARK}",
    }


def recommend_sources(name: str, website: str | None) -> tuple[str, str]:
    text = f"{name} {website or ''}".lower()
    for keywords, pair in RECOMMENDATIONS:
        if any(k in text for k in keywords):
            return pair
    return ("linear", "slack")


@router.post("/tenant")
def create_tenant(body: TenantIn, request: Request, response: Response) -> dict[str, Any]:
    """Sign-up: the only unauthenticated write. Needs the bootstrap token OR a verified Google id_token.

    Creates tenant + owner (connection bound to the new tenant so RLS WITH CHECK passes for the app role), seeds
    the five brain slices, and returns a session cookie so the caller can continue authenticated.
    """
    owner_email: str | None = None
    owner_name: str | None = None
    if body.bootstrap_token is not None:
        if not bootstrap.enabled():
            raise HTTPException(status_code=404, detail="bootstrap is disabled")
        if not bootstrap.check_token(body.bootstrap_token):
            raise HTTPException(status_code=403, detail="bad bootstrap token")
        owner_email = (body.email or "").strip().lower()
        if not owner_email:
            raise HTTPException(status_code=422, detail="email is required with bootstrap_token")
    elif body.google_id_token:
        try:
            claims = google.verify_id_token(body.google_id_token)  # nonce optional on this path
        except google.GoogleAuthError:
            raise HTTPException(status_code=403, detail="invalid Google id_token") from None
        owner_email = str(claims["email"]).lower()
        owner_name = claims.get("name")
        if body.email and body.email.strip().lower() != owner_email:
            raise HTTPException(status_code=403, detail="email does not match the Google account")
    else:
        raise HTTPException(
            status_code=401,
            detail="bootstrap_token or google_id_token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    tenant_id = slugify(body.name)
    website = _normalize_site(body.website)
    with get_conn(api_dsn(), tenant_id=tenant_id) as conn:
        conn.execute(
            """INSERT INTO tenants (id, name, website, timezone) VALUES (%s, %s, %s, coalesce(%s, 'America/Los_Angeles'))
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, website = coalesce(EXCLUDED.website, tenants.website)""",
            (tenant_id, body.name, website, body.timezone),
        )
        if conn.execute("SELECT count(*) AS n FROM users WHERE tenant_id = %s", (tenant_id,)).fetchone()["n"]:
            # An existing company: only its members may re-run sign-up (no takeover of a slug by a stranger).
            member = conn.execute(
                "SELECT id FROM users WHERE tenant_id = %s AND lower(email) = %s", (tenant_id, owner_email)
            ).fetchone()
            if not member:
                raise HTTPException(status_code=409, detail="a company with this name already exists")
        user = bootstrap.upsert_owner(conn, tenant_id, owner_email, owner_name or body.name_of_owner)
        seeded = 0
        for slice_name, content in template_slices(body.name, website).items():
            # Seed only when nothing exists for the slice; never overwrite a human-confirmed doc.
            cur = conn.execute(
                """INSERT INTO brain_docs (tenant_id, path, slice, content, version, source)
                   VALUES (%s, %s, %s, %s, 1, 'extracted') ON CONFLICT (tenant_id, path) DO NOTHING""",
                (tenant_id, f"{slice_name}.md", slice_name, content),
            )
            seeded += cur.rowcount
        row = conn.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
        _, cookie_value = sessions.create_session(conn, tenant_id, user["id"], request.headers.get("user-agent"))
    set_session_cookie(response, request, cookie_value)
    return {
        "tenant": dict(row),
        "owner_email": owner_email,
        "user": {k: user[k] for k in ("id", "email", "name", "tenant_id", "role")},
        "seeded_slices": seeded,
        "recommended_sources": list(recommend_sources(body.name, website)),
        "sources": list(SOURCES),
    }


@router.post("/connections")
def upsert_connection(
    body: ConnectionIn, conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)
) -> dict[str, Any]:
    source = body.source.lower().strip()
    if source not in SOURCES:
        raise HTTPException(status_code=422, detail=f"unknown source {source!r}; one of {', '.join(SOURCES)}")
    ref = body.secret_ref.strip()
    if not (ref.startswith("env:") or ref.startswith("kv:")) or len(ref) < 5:
        raise HTTPException(
            status_code=422, detail="secret_ref must be 'env:NAME' or 'kv:NAME' — never the secret itself"
        )
    if not conn.execute("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)).fetchone():
        raise HTTPException(status_code=404, detail="tenant not found; POST /onboarding/tenant first")
    row = conn.execute(
        """INSERT INTO connections (id, tenant_id, source, secret_ref, config, status)
           VALUES (%s, %s, %s, %s, %s::jsonb, 'connected')
           ON CONFLICT (tenant_id, source) DO UPDATE
             SET secret_ref = EXCLUDED.secret_ref, config = EXCLUDED.config, status = 'connected', last_error = NULL
           RETURNING id, tenant_id, source, secret_ref, config, status, last_sync_at, created_at""",
        (f"{tenant_id}:{source}", tenant_id, source, ref, json.dumps(body.config)),
    ).fetchone()
    return dict(row)


@router.get("/connections")
def list_connections(conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)) -> list[dict]:
    rows = conn.execute(
        "SELECT id, source, secret_ref, config, status, last_sync_at, last_error FROM connections WHERE tenant_id=%s ORDER BY source",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- compile -------------------------------------------------------------------


def _counts(conn: psycopg.Connection, t: str) -> dict[str, int]:
    return {
        table: scalar(conn, f"SELECT count(*) FROM {table} WHERE tenant_id=%s", (t,))  # noqa: S608 - table names are constants
        for table in (
            "issues",
            "projects",
            "messages",
            "transactions",
            "bills",
            "vendors",
            "deployments",
            "sequences",
            "accounts",
            "documents",
            "signals",
        )
    }


def _existing(conn: psycopg.Connection, t: str, slice_name: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT content, source, version FROM brain_docs WHERE tenant_id=%s AND slice=%s ORDER BY version DESC LIMIT 1",
        (t, slice_name),
    ).fetchone()


def compile_cards(conn: psycopg.Connection, tenant_id: str) -> tuple[list[MemoryCard], dict[str, int]]:
    tenant = conn.execute("SELECT * FROM tenants WHERE id=%s", (tenant_id,)).fetchone()
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")
    counts = _counts(conn, tenant_id)
    cards: list[MemoryCard] = []

    # identity — tenant row + existing extracted/human doc
    ident = _existing(conn, tenant_id, "identity")
    sources = [f"tenants:{tenant_id}"]
    draft = ident["content"] if ident else f"# Identity\n\n{tenant['name']} — {tenant['website'] or ''}\n\n{DRAFT_MARK}"
    if ident:
        sources.append(f"brain_docs:identity.md v{ident['version']} ({ident['source']})")
    cards.append(MemoryCard(slice="identity", draft=draft, sources=sources))

    # icp — from accounts (stage, icp_score) and customer labels
    accts = conn.execute(
        "SELECT name, stage, icp_score FROM accounts WHERE tenant_id=%s ORDER BY icp_score DESC NULLS LAST, name LIMIT 12",
        (tenant_id,),
    ).fetchall()
    icp_doc = _existing(conn, tenant_id, "icp")
    if accts:
        lines = [
            f"- {a['name']} — {a['stage'] or 'stage unknown'}"
            + (f" · ICP {a['icp_score']}" if a["icp_score"] is not None else "")
            for a in accts
        ]
        draft = (
            "# Ideal customer profile\n\nAccounts we already track (highest ICP first):\n"
            + "\n".join(lines)
            + f"\n\n{DRAFT_MARK}"
        )
        sources = [f"accounts:{a['name']}" for a in accts]
    elif icp_doc and icp_doc["source"] == "human":
        draft, sources = icp_doc["content"], [f"brain_docs:icp.md v{icp_doc['version']} (human)"]
    else:
        draft = f"# Ideal customer profile\n\nNo accounts ingested yet — describe who buys from {tenant['name']}.\n\n{DRAFT_MARK}"
        sources = ["accounts:(empty)"]
    cards.append(MemoryCard(slice="icp", draft=draft, sources=sources))

    # voice — from human-authored Slack messages (authors + volume) or the existing doc
    voice_doc = _existing(conn, tenant_id, "voice")
    msg = conn.execute(
        "SELECT channel, count(*) AS n FROM messages WHERE tenant_id=%s GROUP BY channel ORDER BY n DESC LIMIT 5",
        (tenant_id,),
    ).fetchall()
    if voice_doc and voice_doc["source"] == "human":
        draft, sources = voice_doc["content"], [f"brain_docs:voice.md v{voice_doc['version']} (human)"]
    elif msg:
        chans = ", ".join(f"#{m['channel'] or 'unknown'} ({m['n']})" for m in msg)
        draft = f"# Brand voice\n\nRead {counts['messages']} messages across {chans}. Tone extraction needs the daemon (Tier 1) — describe the voice here for now.\n\n{DRAFT_MARK}"
        sources = [f"messages:{m['channel']}" for m in msg]
    else:
        draft = voice_doc["content"] if voice_doc else f"# Brand voice\n\n{DRAFT_MARK}"
        sources = ["messages:(empty)"]
    cards.append(MemoryCard(slice="voice", draft=draft, sources=sources))

    # pricing — brain_docs pricing.md + observed inflows
    pricing_doc = _existing(conn, tenant_id, "pricing")
    inflows = conn.execute(
        "SELECT counterparty, count(*) AS n, sum(amount) AS total FROM transactions WHERE tenant_id=%s AND amount>0 GROUP BY counterparty ORDER BY total DESC LIMIT 5",
        (tenant_id,),
    ).fetchall()
    draft = pricing_doc["content"] if pricing_doc else f"# Pricing & offer\n\n{DRAFT_MARK}"
    sources = [f"brain_docs:pricing.md v{pricing_doc['version']} ({pricing_doc['source']})"] if pricing_doc else []
    if inflows and not (pricing_doc and pricing_doc["source"] == "human"):
        lines = [
            f"- {i['counterparty'] or 'unknown payer'} — {i['n']} payments · {i['total']:.2f} USD" for i in inflows
        ]
        draft = (
            draft.replace(f"\n\n{DRAFT_MARK}", "")
            + "\n\nObserved inflows (Brex):\n"
            + "\n".join(lines)
            + f"\n\n{DRAFT_MARK}"
        )
        sources += [f"transactions:{i['counterparty']}" for i in inflows]
    cards.append(MemoryCard(slice="pricing", draft=draft, sources=sources or ["brain_docs:pricing.md (missing)"]))

    # team — distinct issue assignees + project leads + users
    team_doc = _existing(conn, tenant_id, "team")
    people = conn.execute(
        """SELECT assignee AS name, count(*) AS n, array_agg(DISTINCT project) FILTER (WHERE project IS NOT NULL) AS projects
             FROM issues WHERE tenant_id=%s AND assignee IS NOT NULL GROUP BY assignee ORDER BY n DESC""",
        (tenant_id,),
    ).fetchall()
    leads = conn.execute(
        "SELECT lead, array_agg(name) AS projects FROM projects WHERE tenant_id=%s AND lead IS NOT NULL GROUP BY lead",
        (tenant_id,),
    ).fetchall()
    owners = conn.execute(
        "SELECT email, role FROM users WHERE tenant_id=%s ORDER BY role, email", (tenant_id,)
    ).fetchall()
    if team_doc and team_doc["source"] == "human":
        draft, sources = team_doc["content"], [f"brain_docs:team.md v{team_doc['version']} (human)"]
    elif people or leads or owners:
        lines = [
            f"- {p['name']} — {p['n']} issues" + (f" · {', '.join(p['projects'][:3])}" if p["projects"] else "")
            for p in people
        ]
        lines += [
            f"- {ld['lead']} — lead of {', '.join(ld['projects'])}"
            for ld in leads
            if ld["lead"] not in {p["name"] for p in people}
        ]
        lines += [f"- {o['email']} — {o['role']} (StartupOS)" for o in owners]
        draft = "# Team\n\nPeople observed in Linear and StartupOS:\n" + "\n".join(lines) + f"\n\n{DRAFT_MARK}"
        sources = (
            [f"issues.assignee:{p['name']}" for p in people]
            + [f"projects.lead:{ld['lead']}" for ld in leads]
            + [f"users:{o['email']}" for o in owners]
        )
    else:
        draft = team_doc["content"] if team_doc else f"# Team\n\n{DRAFT_MARK}"
        sources = ["issues.assignee:(empty)"]
    cards.append(MemoryCard(slice="team", draft=draft, sources=sources))
    return cards, counts


@router.post("/compile")
def compile_onboarding(
    conn: psycopg.Connection = Depends(get_db),
    tenant_id: str = Depends(get_tenant),
    principal: Principal = Depends(current_principal),
) -> dict[str, Any]:
    cards, counts = compile_cards(conn, tenant_id)
    outcome = (
        "Read " + ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        if any(counts.values())
        else "Read 0 rows — connect a source and run ingest"
    )
    # Tier-0 ledger row: no model, zero cost. Marks the 'compiled' onboarding step.
    conn.execute(
        """INSERT INTO runs (id, tenant_id, trigger, skill, tier, model, status, outcome, finished_at, acted_by)
           VALUES (%s, %s, 'ask', %s, 0, NULL, 'ok', %s, %s, %s)""",
        (run_id(), tenant_id, COMPILE_SKILL, outcome, now_utc(), principal.user_id),
    )
    return {"cards": [c.model_dump() for c in cards], "counts": counts, "outcome": outcome, "tier": 0}


@router.post("/confirm")
def confirm_card(
    body: ConfirmIn, conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)
) -> dict[str, Any]:
    if not conn.execute("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)).fetchone():
        raise HTTPException(status_code=404, detail="tenant not found")
    row = conn.execute(
        """INSERT INTO brain_docs (tenant_id, path, slice, content, version, source, updated_at)
           VALUES (%s, %s, %s, %s, 1, 'human', %s)
           ON CONFLICT (tenant_id, path) DO UPDATE
             SET content = EXCLUDED.content, version = brain_docs.version + 1, source = 'human', updated_at = EXCLUDED.updated_at
           RETURNING path, slice, version, source, updated_at""",
        (tenant_id, f"{body.slice}.md", body.slice, body.content, now_utc()),
    ).fetchone()
    return dict(row)


@router.post("/cadence")
def set_cadence(
    body: CadenceIn, conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)
) -> dict[str, Any]:
    if not conn.execute(
        "UPDATE tenants SET timezone=%s, pulse_hour=%s, pulse_channel=%s WHERE id=%s RETURNING id",
        (body.timezone, body.pulse_hour, body.channel, tenant_id),
    ).fetchone():
        raise HTTPException(status_code=404, detail="tenant not found")
    month = date.today().replace(day=1)
    conn.execute(
        """INSERT INTO budgets (tenant_id, month, tier2_tokens_allowed) VALUES (%s, %s, %s)
           ON CONFLICT (tenant_id, month) DO UPDATE SET tier2_tokens_allowed = EXCLUDED.tier2_tokens_allowed""",
        (tenant_id, month, body.tier2_tokens_allowed),
    )
    return {
        "tenant_id": tenant_id,
        "timezone": body.timezone,
        "pulse_hour": body.pulse_hour,
        "channel": body.channel,
        "month": month.isoformat(),
        "tier2_tokens_allowed": body.tier2_tokens_allowed,
    }


@router.get("/status")
def onboarding_status(
    conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)
) -> dict[str, Any]:
    tenant = conn.execute("SELECT * FROM tenants WHERE id=%s", (tenant_id,)).fetchone()
    if not tenant:
        return {
            "tenant_id": tenant_id,
            "steps": {
                "tenant": False,
                "connections": False,
                "compiled": False,
                "cards": False,
                "pulse": False,
                "cadence": False,
            },
            "connections": 0,
            "cards_confirmed": [],
            "done": 0,
            "total": 6,
        }
    n_conn = scalar(conn, "SELECT count(*) FROM connections WHERE tenant_id=%s AND status='connected'", (tenant_id,))
    compiled = scalar(conn, "SELECT count(*) FROM runs WHERE tenant_id=%s AND skill=%s", (tenant_id, COMPILE_SKILL)) > 0
    confirmed = [
        r["slice"]
        for r in conn.execute(
            "SELECT slice FROM brain_docs WHERE tenant_id=%s AND source='human' AND slice = ANY(%s)",
            (tenant_id, list(SLICES)),
        ).fetchall()
    ]
    pulse = (
        scalar(conn, "SELECT count(*) FROM runs WHERE tenant_id=%s AND skill='cockpit.morning_pulse'", (tenant_id,)) > 0
    )
    cadence = (
        scalar(
            conn,
            "SELECT count(*) FROM budgets WHERE tenant_id=%s AND month=date_trunc('month', now())::date",
            (tenant_id,),
        )
        > 0
    )
    steps = {
        "tenant": True,
        "connections": n_conn >= 2,
        "compiled": compiled,
        "cards": len(set(confirmed)) == len(SLICES),
        "pulse": pulse,
        "cadence": cadence,
    }
    return {
        "tenant_id": tenant_id,
        "tenant": dict(tenant),
        "steps": steps,
        "connections": n_conn,
        "cards_confirmed": sorted(set(confirmed)),
        "done": sum(steps.values()),
        "total": len(steps),
    }
