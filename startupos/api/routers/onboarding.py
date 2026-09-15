"""Onboarding (Day 0): tenant → connections → compile → confirm cards → cadence → status. Deterministic, no model.

Sprint 3a (Track O): `POST /onboarding/compile` no longer waits for anything. It returns the five cards from whatever
is in the DB right now and enqueues the first-pulse chain in `tenant_jobs` (backfill per connected source → signals →
context_pack → chief_of_staff → morning_pulse); the daemon's job service runs it. `GET /onboarding/status` reports the
chain (`jobs`) and `first_pulse_ready` so the wizard can show progress instead of "compiling…".
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from api import ratelimit
from api.deps import api_dsn, current_principal, get_db, get_tenant, now_utc, scalar, set_session_cookie
from auth import bootstrap, google, sessions
from auth import config as auth_config
from auth.identity import Principal
from common import jobs as job_queue
from common import secrets
from common import tenants as tenant_cfg
from common.db import get_conn
from common.ids import run_id
from common.models import MemoryCard
from common.settings import settings

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

SOURCES = ("linear", "slack", "brex", "apollo", "vercel", "posthog", "github", "gdrive", "gmail", "stripe")
SLICES: tuple[str, ...] = ("identity", "icp", "voice", "pricing", "team")
DRAFT_MARK = "Draft — confirm or edit."
COMPILE_SKILL = "onboarding.compile"
DEFAULT_TIER2_TOKENS = 1_500_000  # the monthly Tier-2 budget an OPERATOR-created company starts with
# A self-serve company (a stranger with the public URL, signed up with Google) starts on the `self_serve` tier
# instead, whose allowance is STARTUPOS_SIGNUP_TIER2_TOKENS (daemon/budget.py). Sprint 3d PE review: rate
# limiting caps how FAST strangers arrive; only the tier caps what each one can then spend.

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
    # (or the same id_token in the signed `sos_signup` cookie the Google callback sets for a brand-new identity)


class ConnectionIn(BaseModel):
    source: str
    # The key itself → encrypted at rest as kv:<source>_api_key. repr=False keeps it out of any log line.
    credential: str | None = Field(default=None, repr=False)
    secret_ref: str | None = None  # or a reference the operator manages ('env:NAME' / 'kv:NAME')
    config: dict[str, Any] = {}


class ConfirmIn(BaseModel):
    slice: Literal["identity", "icp", "voice", "pricing", "team"]
    content: str = Field(min_length=1)


class CadenceIn(BaseModel):
    """Every field is optional and means "leave this as it is" when omitted.

    It used to mean "reset to the default", which was harmless while the wizard was the only caller and always
    sent all four. Settings now edits one field at a time (the Slack channel), and silently resetting a founder's
    timezone, pulse hour or monthly budget because the form did not resend them would be a bug.
    """

    timezone: str | None = None
    pulse_hour: int | None = Field(default=None, ge=0, le=23)
    channel: Literal["web", "slack", "both"] | None = None
    tier2_tokens_allowed: int | None = Field(default=None, ge=0)
    # Which Slack channel StartupOS posts in (`#name` or a channel id). Send null/"" to unset it, which means
    # "post where the founder put StartupOS while installing Slack" (`slack_installations.default_channel`).
    slack_channel: str | None = None


def _budget():
    """daemon.budget, imported lazily — the API must not depend on the daemon at import time (see slack.py)."""
    from daemon import budget

    return budget


def tier2_ceiling(conn: psycopg.Connection, tenant_id: str) -> int:
    """The most Tier-2 tokens this tenant may be allowed in a month: its tier's allowance, nothing more.

    The cadence form is inside the tenant, so without a ceiling a self-serve company could raise its own budget
    to any number it liked and the cap would be decoration. Raising it is an operator action (`UPDATE tenants
    SET tier = ...`), the same act that grants the allowance in the first place.
    """
    return _budget().allowed_for(conn, tenant_id)


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


# Sign-up is the one unauthenticated write on a publicly reachable host, it checks the same bootstrap token as
# POST /auth/bootstrap, and every company it creates arrives with its own monthly Tier-2 token budget that the
# daemon will spend. So it is rate-limited like the bootstrap route it shares a secret with — an unauthenticated
# route that costs money is as bad as one that leaks data. 5 companies a minute from one source is far more than
# a founder needs and far less than an abuser wants.
_signup_throttle = ratelimit.Throttle(name="onboarding.tenant")


@router.post("/tenant")
def create_tenant(body: TenantIn, request: Request, response: Response) -> dict[str, Any]:
    """Sign-up: the only unauthenticated write. Needs the bootstrap token OR a verified Google id_token.

    Creates tenant + owner (connection bound to the new tenant so RLS WITH CHECK passes for the app role), seeds
    the five brain slices, and returns a session cookie so the caller can continue authenticated.
    """
    if not _signup_throttle.allow(ratelimit.client_key(request)):
        raise HTTPException(status_code=429, detail="too many sign-up attempts; wait a minute")
    owner_email: str | None = None
    owner_name: str | None = None
    google_sub: str | None = None
    if body.bootstrap_token is not None:
        if not bootstrap.enabled():
            raise HTTPException(status_code=404, detail="bootstrap is disabled")
        if not bootstrap.check_token(body.bootstrap_token):
            raise HTTPException(status_code=403, detail="bad bootstrap token")
        owner_email = (body.email or "").strip().lower()
        if not owner_email:
            raise HTTPException(status_code=422, detail="email is required with bootstrap_token")
    elif id_token := (body.google_id_token or google.read_signup_cookie(request.cookies.get(google.SIGNUP_COOKIE))):
        # Self-serve sign-up. The operator can close this door entirely (STARTUPOS_ALLOW_SIGNUP=0) without
        # touching the rest of sign-in: existing people keep signing in, and the operator keeps creating
        # companies with the bootstrap token. Checked before the token is verified so a closed install does no
        # work for a stranger, and answered in copy a person can act on rather than a bare 403.
        if not auth_config.allow_signup():
            raise HTTPException(
                status_code=403,
                detail=(
                    "This StartupOS installation is not accepting new companies right now, so none was "
                    "created. If you were invited to a company that is already here, ask its owner to invite "
                    "your address and then sign in; otherwise ask whoever runs this installation."
                ),
            )
        # Either the caller hands us the id_token, or Either the caller hands us the id_token, or it is the one the Google callback put
        # in `sos_signup` after verifying an identity that matched no user (Sprint 3d, Track G) — a cookie so it
        # never reaches JavaScript, the URL bar or a log. Either way it is verified here, again, before it counts.
        try:
            claims = google.verify_id_token(id_token)  # nonce optional on this path
        except google.UnverifiedEmailError:
            raise HTTPException(status_code=403, detail="Google has not verified this e-mail address") from None
        except google.GoogleAuthError:
            raise HTTPException(status_code=403, detail="invalid Google id_token") from None
        owner_email = str(claims["email"]).lower()
        owner_name = claims.get("name")
        google_sub = str(claims.get("sub") or "") or None
        if body.email and body.email.strip().lower() != owner_email:
            raise HTTPException(status_code=403, detail="email does not match the Google account")
    else:
        raise HTTPException(
            status_code=401,
            detail=(
                "Sign in with Google first — this sign-up has expired or was never started. "
                "Go back to the sign-in page and continue with Google."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )

    tenant_id = slugify(body.name)
    website = _normalize_site(body.website)
    # The tier IS the budget (daemon/budget.py reads nothing else), so a self-serve company is created on the
    # small self-serve tier and an operator-created one is unchanged. Only ever set on INSERT below: re-running
    # sign-up must not silently downgrade a company an operator has since promoted.
    tier = _budget().SELF_SERVE_TIER if google_sub else "founder"
    if google_sub:  # the Google sign-up path only; the bootstrap token is the operator's own install
        # "An address that already belongs to a company never gets a second, empty one" (CONTRACTS.md Sprint 3d).
        # GET /auth/google/callback enforces that by only ever issuing the `sos_signup` cookie for an identity it
        # found no user for — but `google_id_token` is a documented field on this endpoint too, and that path
        # skipped the check entirely. Without it, one Google account is an unbounded tenant-minting primitive on
        # a public host (and a quiet way for a person to end up with two workspaces and no idea which is which).
        # Re-running sign-up for the company you already belong to stays allowed: that is the wizard going back.
        with get_conn(api_dsn(), tenant_id="") as anon:
            existing = anon.execute("SELECT * FROM auth_lookup_google(%s, %s)", (google_sub, owner_email)).fetchone()
        if existing and existing["tenant_id"] != tenant_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    f"{owner_email} already has a StartupOS account. Sign in with Google instead — "
                    "an address that belongs to a company does not get a second, empty one."
                ),
            )
    with get_conn(api_dsn(), tenant_id=tenant_id) as conn:
        conn.execute(
            """INSERT INTO tenants (id, name, website, timezone, tier)
               VALUES (%s, %s, %s, coalesce(%s, 'America/Los_Angeles'), %s)
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, website = coalesce(EXCLUDED.website, tenants.website)""",
            (tenant_id, body.name, website, body.timezone, tier),
        )
        if conn.execute("SELECT count(*) AS n FROM users WHERE tenant_id = %s", (tenant_id,)).fetchone()["n"]:
            # An existing company: only its members may re-run sign-up (no takeover of a slug by a stranger).
            member = conn.execute(
                "SELECT id FROM users WHERE tenant_id = %s AND lower(email) = %s", (tenant_id, owner_email)
            ).fetchone()
            if not member:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"A company called {body.name!r} is already on StartupOS. If that is your company, ask "
                        f"whoever set it up to invite {owner_email} — companies are invite-only. Otherwise pick "
                        "a different name; you will get your own, separate company."
                    ),
                )
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
    # The sign-up cookie has done its one job; a leftover would let a stale identity create a second company.
    response.delete_cookie(google.SIGNUP_COOKIE, path=auth_config.cookie_path("/onboarding"))
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
    """Connect a source. Either hand us the key (`credential`) — stored encrypted for this tenant only, referenced as
    `kv:<source>_api_key` — or a `secret_ref` the operator manages. The plaintext is never logged or returned."""
    source = body.source.lower().strip()
    if source not in SOURCES:
        raise HTTPException(status_code=422, detail=f"unknown source {source!r}; one of {', '.join(SOURCES)}")
    credential = body.credential.strip() if body.credential is not None else None
    if credential is not None and not credential:
        raise HTTPException(status_code=422, detail="credential must not be empty")
    if credential is not None:
        ref = f"kv:{source}_api_key"
    else:
        ref = (body.secret_ref or "").strip()
        if not (ref.startswith("env:") or ref.startswith("kv:")) or len(ref) < 5:
            raise HTTPException(
                status_code=422,
                detail="give `credential` (the key) or a secret_ref 'env:NAME' / 'kv:NAME' — never the key as a ref",
            )
        # env: refs are the operator's own keys (the install tenant, TENANT_ID). Any other tenant pointing a
        # connection at them would ingest and act with the operator's Linear/Slack/Brex — refuse, never fall through.
        if ref.startswith("env:") and not secrets.env_ref_allowed(tenant_id, source, ref):
            raise HTTPException(
                status_code=403,
                detail="env: secret refs are operator-managed and not available to this tenant; pass `credential`",
            )
    if not conn.execute("SELECT 1 FROM tenants WHERE id = %s", (tenant_id,)).fetchone():
        raise HTTPException(status_code=404, detail="tenant not found; POST /onboarding/tenant first")
    if credential is not None:
        try:
            secrets.put(conn, tenant_id, ref[len("kv:") :], credential)
        except secrets.SecretsUnavailable:
            raise HTTPException(
                status_code=503, detail="secret storage is not configured (STARTUPOS_MASTER_KEY); ask your operator"
            ) from None
    row = conn.execute(
        """INSERT INTO connections (id, tenant_id, source, secret_ref, config, status)
           VALUES (%s, %s, %s, %s, %s::jsonb, 'connected')
           ON CONFLICT (tenant_id, source) DO UPDATE
             SET secret_ref = EXCLUDED.secret_ref, config = EXCLUDED.config, status = 'connected', last_error = NULL
           RETURNING id, tenant_id, source, secret_ref, config, status, last_sync_at, created_at""",
        (f"{tenant_id}:{source}", tenant_id, source, ref, json.dumps(body.config)),
    ).fetchone()
    out = dict(row)
    out["has_credential"] = _has_credential(conn, tenant_id, ref)
    return out


def _has_credential(conn: psycopg.Connection, tenant_id: str, ref: str | None) -> bool:
    """Does StartupOS hold a usable key for this ref? kv: → an encrypted row exists for this tenant (no decrypt,
    no master key needed); env: → the variable is set in the service environment. Never the value itself."""
    scheme, _, name = (ref or "").partition(":")
    if scheme == "kv" and name:
        return secrets.exists(conn, tenant_id, name)
    if scheme == "env" and name:
        # Only the install tenant may use env refs; for anyone else the answer is False (no env probing either).
        return tenant_id == settings.tenant_id and bool(settings.secret(ref))
    return False


@router.get("/connections")
def list_connections(conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)) -> list[dict]:
    """Connections with `secret_ref` and `has_credential` only — a credential is write-only through this API."""
    rows = conn.execute(
        "SELECT id, source, secret_ref, config, status, last_sync_at, last_error FROM connections WHERE tenant_id=%s ORDER BY source",
        (tenant_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["has_credential"] = _has_credential(conn, tenant_id, d.get("secret_ref"))
        out.append(d)
    return out


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
    """Five cards from the DB as it is now + the first-pulse job chain, enqueued (idempotent) for the daemon.

    Returns immediately: the backfill, signals, context pack, Chief of Staff and morning pulse run in the daemon's
    job service; poll GET /onboarding/status for progress. Re-running compile while jobs are queued/running adds
    nothing (one active job per kind per tenant); after the chain finished it queues a fresh one.
    """
    cards, counts = compile_cards(conn, tenant_id)
    jobs = job_queue.enqueue_chain(conn, tenant_id)
    queued = [j["kind"] for j in jobs if j["enqueued"]]
    outcome = (
        "Read " + ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        if any(counts.values())
        else "Read 0 rows — connect a source and run ingest"
    )
    if queued:
        outcome += f"; queued {len(queued)} jobs: {', '.join(queued)}"
    # Tier-0 ledger row: no model, zero cost. Marks the 'compiled' onboarding step.
    conn.execute(
        """INSERT INTO runs (id, tenant_id, trigger, skill, tier, model, status, outcome, finished_at, acted_by)
           VALUES (%s, %s, 'ask', %s, 0, NULL, 'ok', %s, %s, %s)""",
        (run_id(), tenant_id, COMPILE_SKILL, outcome, now_utc(), principal.user_id),
    )
    return {
        "cards": [c.model_dump() for c in cards],
        "counts": counts,
        "jobs": [{**job_queue.public(j), "enqueued": j["enqueued"]} for j in jobs],
        "outcome": outcome,
        "tier": 0,
    }


@router.get("/cards")
def read_cards(conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant)) -> dict[str, Any]:
    """The five cards + counts from the DB as it is now — read-only (no jobs queued, no ledger row). The wizard
    re-reads these once the backfill landed instead of re-compiling, which would queue another chain."""
    cards, counts = compile_cards(conn, tenant_id)
    return {"cards": [c.model_dump() for c in cards], "counts": counts}


@router.get("/jobs")
def list_jobs(
    conn: psycopg.Connection = Depends(get_db), tenant_id: str = Depends(get_tenant), limit: int = 50
) -> list[dict[str, Any]]:
    """The tenant's job rows, newest first (operators; the wizard reads the summary in /status)."""
    rows = conn.execute(
        f"SELECT {job_queue.JOB_COLUMNS} FROM tenant_jobs WHERE tenant_id = %s ORDER BY created_at DESC, id DESC LIMIT %s",
        (tenant_id, max(1, min(limit, 500))),
    ).fetchall()
    return [job_queue.public(dict(r)) for r in rows]


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
    """Persist the cadence, and the Slack channel the founder wants StartupOS to post in.

    The channel is validated here (`#name` or a channel id) so a typo is a 422 the founder can see, not a
    `channel_not_found` discovered by the daemon at 7am. Unset (null/"") means "use the install's channel".
    """
    try:
        slack_channel = tenant_cfg.normalize_slack_channel(body.slack_channel)
    except tenant_cfg.BadChannel as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    columns: dict[str, Any] = {}
    if body.timezone is not None:
        columns["timezone"] = body.timezone
    if body.pulse_hour is not None:
        columns["pulse_hour"] = body.pulse_hour
    if body.channel is not None:
        columns["pulse_channel"] = body.channel
    if "slack_channel" in body.model_fields_set:
        columns["slack_channel"] = slack_channel
    if columns:
        assignments = ", ".join(f"{name} = %s" for name in columns)  # names are literals above, never input
        row = conn.execute(
            f"UPDATE tenants SET {assignments} WHERE id = %s RETURNING timezone, pulse_hour, pulse_channel, slack_channel",  # noqa: S608
            (*columns.values(), tenant_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT timezone, pulse_hour, pulse_channel, slack_channel FROM tenants WHERE id = %s", (tenant_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="tenant not found")

    month = date.today().replace(day=1)
    ceiling = tier2_ceiling(conn, tenant_id)
    allowed = body.tier2_tokens_allowed
    if allowed is not None and allowed > ceiling:
        raise HTTPException(
            status_code=422,
            detail=(
                f"This company's plan allows at most {ceiling:,} Tier-2 tokens a month. Ask whoever runs this "
                "StartupOS installation to raise your tier if you need more."
            ),
        )
    if allowed is None:
        allowed = scalar(
            conn, "SELECT max(tier2_tokens_allowed) FROM budgets WHERE tenant_id=%s AND month=%s", (tenant_id, month)
        )
    if allowed is None:
        allowed = min(DEFAULT_TIER2_TOKENS, ceiling)
    conn.execute(
        """INSERT INTO budgets (tenant_id, month, tier2_tokens_allowed) VALUES (%s, %s, %s)
           ON CONFLICT (tenant_id, month) DO UPDATE SET tier2_tokens_allowed = EXCLUDED.tier2_tokens_allowed""",
        (tenant_id, month, allowed),
    )
    return {
        "tenant_id": tenant_id,
        "timezone": row["timezone"],
        "pulse_hour": row["pulse_hour"],
        "channel": row["pulse_channel"],
        "slack_channel": row["slack_channel"],
        "month": month.isoformat(),
        "tier2_tokens_allowed": allowed,
        "tier2_tokens_ceiling": ceiling,
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
            "jobs": {"queued": 0, "running": 0, "done": 0, "failed": 0, "last_error": None, "chain": []},
            "first_pulse_ready": False,
            "tier2_tokens_ceiling": tier2_ceiling(conn, tenant_id),
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
        # Track O: the first-pulse chain as the daemon's job service sees it, and whether a pulse run exists yet.
        "jobs": job_queue.status_summary(conn, tenant_id),
        "first_pulse_ready": pulse,
        # What this tenant's tier allows per month, so the cadence form offers a number it can actually save
        # (a self-serve company's ceiling is far below the founder default the wizard used to hardcode).
        "tier2_tokens_ceiling": tier2_ceiling(conn, tenant_id),
    }
