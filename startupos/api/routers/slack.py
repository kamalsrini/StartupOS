"""Slack install and inbound HTTP (Sprint 3b, Track I).

Four routes, two of them public because Slack calls them with no session:

    GET  /slack/install            authenticated → 302 to Slack's oauth/v2/authorize (state = signed tenant + nonce)
    GET  /slack/oauth/callback     public        → verify state, exchange the code, store the install, redirect
    POST /slack/events             public        → signature-verified; challenge echo, uninstall, message → queue
    POST /slack/interactivity      public        → signature-verified; dispatch to daemon/slack_actions.py

The two public routes are authenticated by Slack's own HMAC signature (`auth/slack_sig.py`), not by a principal,
and the tenant of every inbound request is resolved ONLY from the Slack `team_id` through the installation table
(SECURITY DEFINER `slack_lookup_install`) — never from a header, a query parameter or a body field the caller
controls. An unknown or revoked workspace gets a 200 with an ephemeral "not connected" so Slack stops retrying.

Slack gives a handler 3 seconds. `/slack/events` therefore does no work: it enqueues a `slack_event` row in
`tenant_jobs` and returns; the daemon's `service_jobs()` answers and posts the reply with the tenant's own bot
token. Nothing here logs a code, a token, a signature or a message body.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlencode

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from api.deps import api_dsn, current_principal
from auth import config, slack_install, slack_sig
from auth.identity import Principal
from common import jobs as job_queue
from common import secrets
from common.db import get_conn
from common.settings import settings

log = logging.getLogger("api.slack")

router = APIRouter(prefix="/slack", tags=["slack"])

NOT_CONNECTED = "This Slack workspace is not connected to StartupOS. Ask an owner to run Connect Slack in Settings."
# An unexpected handler failure: 200 + ephemeral, never a 5xx (Slack would retry it and show the presser an error).
INTERACTIVITY_FAILED = (
    "Something went wrong handling that button. The cockpit still has this approval — open it there to check."
)
# Events we act on. Everything else is acknowledged and dropped (Slack sends a lot we did not ask for).
UNINSTALL_EVENTS = ("app_uninstalled", "tokens_revoked")
MESSAGE_EVENTS = ("app_mention", "message")


def _settings_url(**params: str) -> str:
    return f"{config.web_url()}/settings?{urlencode(params)}"


def _ephemeral(text: str) -> JSONResponse:
    """200 with an ephemeral body: Slack shows it to the caller and does not retry the delivery."""
    return JSONResponse({"response_type": "ephemeral", "text": text})


async def _verified_body(request: Request) -> bytes | JSONResponse:
    """The raw body, once Slack's signature checks out. Returns a JSONResponse to short-circuit on failure."""
    body = await request.body()
    try:
        slack_sig.verify(request.headers, body, settings.slack_signing_secret())
    except slack_sig.SlackSigningNotConfigured:
        log.warning("inbound Slack request refused: STARTUPOS_SLACK_SIGNING_SECRET is not configured")
        return JSONResponse({"detail": "Slack is not configured on this install"}, status_code=503)
    except slack_sig.SlackSignatureError as exc:
        log.warning("inbound Slack request refused: %s", exc)
        return JSONResponse({"detail": "invalid Slack signature"}, status_code=403)
    return body


def _live_install(team_id: str) -> dict[str, Any] | None:
    """The installation for this workspace, or None when unknown or revoked. No tenant is bound for the lookup."""
    if not team_id:
        return None
    with get_conn(api_dsn(), tenant_id="") as anon:
        row = slack_install.lookup_team(anon, team_id)
    if not row or row.get("revoked_at") is not None:
        return None
    return row


# --- install ------------------------------------------------------------------------------------


@router.get("/install")
def install(principal: Principal = Depends(current_principal)) -> Any:
    """Start the OAuth dance for the signed-in user's tenant. 503 when the operator has not configured the app."""
    if not slack_install.configured():
        return JSONResponse(
            {
                "detail": "Slack is not configured on this install (STARTUPOS_SLACK_CLIENT_ID/"
                "STARTUPOS_SLACK_CLIENT_SECRET); ask your operator."
            },
            status_code=503,
        )
    state = slack_install.make_state(principal.tenant_id, principal.user_id)
    return RedirectResponse(slack_install.authorize_url(state), status_code=302)


@router.get("/oauth/callback")
def oauth_callback(state: str | None = None, code: str | None = None, error: str | None = None) -> Any:
    """Slack sends the browser back here. Every failure redirects to the web app with a reason — never a value."""
    if error:
        return RedirectResponse(_settings_url(slack="error", reason="denied"), status_code=302)
    if not slack_install.configured():
        return JSONResponse({"detail": "Slack is not configured on this install"}, status_code=503)
    parsed = slack_install.read_state(state)
    if not parsed or not code:
        return RedirectResponse(_settings_url(slack="error", reason="state"), status_code=302)
    tenant_id = parsed["tenant_id"]
    try:
        oauth = slack_install.exchange_code(code)
    except slack_install.SlackInstallError as exc:
        log.warning("slack install failed for %s: %s", tenant_id, exc)
        return RedirectResponse(_settings_url(slack="error", reason="exchange"), status_code=302)
    try:
        with get_conn(api_dsn(), tenant_id=tenant_id) as conn:
            with get_conn(api_dsn(), tenant_id="") as anon:
                installation = slack_install.save_installation(
                    conn, tenant_id, oauth, installed_by=parsed.get("user_id"), anon_conn=anon
                )
    except slack_install.WorkspaceTaken:
        # team_id is UNIQUE: one workspace, one company. Nothing was written for this tenant.
        return RedirectResponse(_settings_url(slack="error", reason="workspace_taken"), status_code=302)
    except secrets.SecretsUnavailable:
        return RedirectResponse(_settings_url(slack="error", reason="secrets"), status_code=302)
    except slack_install.SlackInstallError as exc:
        log.warning("slack install failed for %s: %s", tenant_id, exc)
        return RedirectResponse(_settings_url(slack="error", reason="install"), status_code=302)
    log.info("slack installed for tenant %s (team %s)", tenant_id, installation["team_id"])
    return RedirectResponse(_settings_url(slack="connected"), status_code=302)


@router.get("/status")
def status(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """What the Settings page renders: is the Slack app configured, and is this tenant's workspace connected.

    Never returns a token — `slack_installations` holds none, and the encrypted secret is not read here.
    """
    from daemon import delivery  # local: the API must not depend on the daemon at import time

    with get_conn(api_dsn(), tenant_id=principal.tenant_id) as conn:
        row = slack_install.get_installation(conn, principal.tenant_id)
        has_token = secrets.exists(conn, principal.tenant_id, slack_install.SECRET_NAME)
        cfg = delivery.tenant_delivery_settings(conn, principal.tenant_id)
        install_channel = delivery.install_channel(conn, principal.tenant_id)
    connected = bool(row and row.get("revoked_at") is None and has_token)
    # The channel actually in effect, by delivery's own precedence: the tenant's override → the install's
    # default_channel. `pulse_channel = web` means nothing is posted at all, whatever the channel says.
    effective = cfg["channel"] or install_channel
    return {
        "configured": slack_install.configured() and slack_install.signing_configured(),
        "connected": connected,
        "team_id": (row or {}).get("team_id"),
        "team_name": (row or {}).get("team_name"),
        "default_channel": (row or {}).get("default_channel"),
        # Where posts land, and why: `slack_channel` is the founder's override (POST /onboarding/cadence),
        # null meaning "wherever the install put us"; `channel` is what delivery would resolve right now.
        "slack_channel": cfg["channel"],
        "channel": effective,
        "pulse_channel": cfg["mode"],
        "delivers": bool(connected and effective and cfg["mode"] in delivery.POSTING_MODES),
        "installed_at": _iso((row or {}).get("installed_at")),
        "revoked_at": _iso((row or {}).get("revoked_at")),
        "scopes": list(slack_install.SCOPES),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


# --- events -------------------------------------------------------------------------------------


def _team_id(body: dict[str, Any]) -> str:
    team = body.get("team_id")
    if team:
        return str(team)
    for auth in body.get("authorizations") or []:
        if isinstance(auth, dict) and auth.get("team_id"):
            return str(auth["team_id"])
    return str(((body.get("event") or {}).get("team")) or "")


def _is_human_message(event: dict[str, Any], bot_user_id: str | None) -> bool:
    """A message we should answer: not the bot's own, not an edit/join/file-share subtype, not empty."""
    if event.get("bot_id") or event.get("subtype"):
        return False
    user = str(event.get("user") or "")
    if not user or (bot_user_id and user == bot_user_id):
        return False
    if event.get("type") == "message" and event.get("channel_type") not in ("im", "group", "channel", "mpim", None):
        return False
    return bool((event.get("text") or "").strip())


def enqueue_slack_event(conn: psycopg.Connection, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Queue one inbound message for the daemon. Idempotent on Slack's `event_id` (Slack retries on any hiccup)."""
    event_id = payload.get("event_id")
    if event_id:
        seen = conn.execute(
            "SELECT id FROM tenant_jobs WHERE tenant_id = %s AND kind = %s AND payload->>'event_id' = %s LIMIT 1",
            (tenant_id, job_queue.SLACK_EVENT, str(event_id)),
        ).fetchone()
        if seen:
            return None
    row = conn.execute(
        "INSERT INTO tenant_jobs (tenant_id, kind, payload) VALUES (%s, %s, %s::jsonb) RETURNING id",
        (tenant_id, job_queue.SLACK_EVENT, json.dumps(payload)),
    ).fetchone()
    return dict(row)


@router.post("/events")
async def events(request: Request) -> Any:
    """Slack Events API. Verified, acknowledged immediately, work handed to the daemon."""
    verified = await _verified_body(request)
    if isinstance(verified, JSONResponse):
        return verified
    try:
        body = json.loads(verified or b"{}")
    except json.JSONDecodeError:
        return JSONResponse({"detail": "malformed event body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "malformed event body"}, status_code=400)

    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge", "")})

    team_id = _team_id(body)
    event = body.get("event") or {}
    event_type = str(event.get("type") or "")

    if event_type in UNINSTALL_EVENTS:
        # Resolve even a revoked install so a repeated uninstall is still a clean 200.
        with get_conn(api_dsn(), tenant_id="") as anon:
            row = slack_install.lookup_team(anon, team_id)
        if row:
            with get_conn(api_dsn(), tenant_id=row["tenant_id"]) as conn:
                slack_install.revoke(conn, row["tenant_id"])
            log.info("slack %s for tenant %s (team %s)", event_type, row["tenant_id"], team_id)
        return JSONResponse({"ok": True, "handled": event_type})

    installation = _live_install(team_id)
    if not installation:
        return _ephemeral(NOT_CONNECTED)
    if event_type not in MESSAGE_EVENTS or not _is_human_message(event, installation.get("bot_user_id")):
        return JSONResponse({"ok": True, "handled": "ignored"})

    payload = {
        "event_id": body.get("event_id"),
        "team_id": team_id,
        "channel": event.get("channel"),
        "thread_ts": event.get("thread_ts"),
        "slack_user_id": event.get("user"),
        "text": event.get("text") or "",
    }
    with get_conn(api_dsn(), tenant_id=installation["tenant_id"]) as conn:
        queued = enqueue_slack_event(conn, installation["tenant_id"], payload)
    return JSONResponse({"ok": True, "queued": bool(queued)})


# --- interactivity -------------------------------------------------------------------------------


@router.post("/interactivity")
async def interactivity(request: Request) -> Any:
    """Button presses and slash-command dialogs. Dispatches to daemon/slack_actions.py (Track B owns the handlers)."""
    verified = await _verified_body(request)
    if isinstance(verified, JSONResponse):
        return verified
    form = parse_qs(verified.decode("utf-8", errors="replace"))
    raw = (form.get("payload") or [""])[0]
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return JSONResponse({"detail": "malformed interactivity payload"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"detail": "malformed interactivity payload"}, status_code=400)

    team_id = str((payload.get("team") or {}).get("id") or payload.get("team_id") or "")
    if not _live_install(team_id):
        return _ephemeral(NOT_CONNECTED)

    from daemon import slack_actions

    def conn_factory(tenant_id: str) -> Any:
        return get_conn(api_dsn(), tenant_id=tenant_id)

    try:
        out = slack_actions.handle_action(payload, conn_factory=conn_factory)
    except slack_actions.UnknownAction as exc:
        log.info("slack interactivity ignored: %s", exc)
        return JSONResponse({"detail": "unknown action"}, status_code=404)
    except Exception as exc:
        # A 5xx makes Slack retry and shows the presser a red error; an ephemeral is honest and final. The
        # approval itself is untouched — the handler decides inside a transaction — so the cockpit still has it.
        log.exception(
            "slack interactivity failed (action %s, team %s): %s",
            ",".join(slack_actions.action_ids(payload)) or "?",
            team_id,
            type(exc).__name__,
        )
        return _ephemeral(INTERACTIVITY_FAILED)
    return JSONResponse(out if isinstance(out, dict) else {"ok": True})
