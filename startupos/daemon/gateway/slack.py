"""Slack gateway (Socket Mode). Commands in DM or @mention:

    approve <id>              → approvals.decide(approve) and execute immediately
    decline <id> <reason>     → approvals.decide(decline, reason)
    pending                   → list pending approvals
    anything else             → ask.answer

Starts only under STARTUPOS_DEV=1, and only when SLACK_APP_TOKEN and SLACK_BOT_TOKEN exist. `handle_text` is
pure and testable. Tokens are read via common.settings and never logged.

Sprint 3b (PE follow-up): Socket Mode is a **dev-only** path. In production Slack arrives over HTTPS
(`POST /slack/events` → `tenant_jobs` → `daemon/jobs.py::run_slack_event`), which posts with the tenant's own bot
token. The Socket Mode listener holds one process-wide operator token, so replying with it on behalf of a
resolved tenant B would post as the operator — `reply_token` therefore resolves the *resolved tenant's* own
credential and falls back to the operator token only for the install tenant.

Tenant resolution (Sprint 3a, Track T): every event resolves its tenant from the Slack user through the
SECURITY DEFINER `auth_lookup_slack` — the tenant is always derived from the identity, for asks as much as for
approvals. Unmapped users get `link_hint()`. The only fallback to `settings.tenant_id` is STARTUPOS_DEV=1.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from typing import Any

import psycopg

from common import secrets
from common.db import get_conn
from common.models import Decision
from common.settings import settings
from common.tenants import dev_mode
from daemon import approvals, executors
from daemon.skills import build_ctx
from daemon.skills.ask import answer as ask_answer

log = logging.getLogger("daemon.gateway.slack")
_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")


def _dsn() -> str:
    return getattr(settings, "app_dsn", None) or settings.database_url


def lookup_slack_user(conn: psycopg.Connection, slack_user_id: str) -> tuple[str, str] | None:
    """Slack user id → (tenant_id, user_id) on an ALREADY OPEN, tenant-unbound connection. None if unmapped.

    The SECURITY DEFINER `auth_lookup_slack` is the one mapping from a Slack identity to a StartupOS user, and
    this is it in the one place. `resolve_slack_user` opens its own connection around it (the Socket Mode
    gateway); callers that already hold an unbound connection — `daemon/slack_actions.py` on the HTTP path —
    call this directly instead of opening a second one.
    """
    if not slack_user_id:
        return None
    row = conn.execute("SELECT * FROM auth_lookup_slack(%s)", (slack_user_id,)).fetchone()
    if not row:
        return None
    return row["tenant_id"], row["user_id"]


def resolve_slack_user(slack_user_id: str, dsn: str | None = None) -> tuple[str, str] | None:
    """Slack user id → (tenant_id, user_id) via the SECURITY DEFINER lookup, with no tenant bound. None if unmapped."""
    if not slack_user_id:
        return None
    with get_conn(dsn or _dsn(), tenant_id="") as anon:
        return lookup_slack_user(anon, slack_user_id)


def resolve_event_tenant(
    slack_user_id: str, *, resolver: Any = None, dev_tenant: str | None = None
) -> tuple[str, str] | None:
    """(tenant_id, acting user) for an inbound event, or None → reply with link_hint().

    Production: only `auth_lookup_slack` — the tenant is derived from the identity, never from settings.
    `dev_tenant` is set by start() only under STARTUPOS_DEV=1: an unmapped user then falls back to it with the
    raw Slack id as the acting user, so a local bot works before /auth/slack/link.
    """
    mapped = (resolver or resolve_slack_user)(slack_user_id)
    if mapped:
        return mapped
    if dev_tenant:
        return dev_tenant, slack_user_id or "slack"
    return None


def link_hint() -> str:
    web_url = getattr(settings, "web_url", None) or os.environ.get("STARTUPOS_WEB_URL") or "http://localhost:3000"
    return f"Link your StartupOS account first: sign in at {web_url} and run /startupos link <your slack id>"


def parse_command(text: str) -> tuple[str, list[str]]:
    """('approve', [id]) | ('decline', [id, reason]) | ('pending', []) | ('ask', [question])."""
    clean = _MENTION.sub("", text or "").strip()
    if not clean:
        return "ask", [""]
    try:
        parts = shlex.split(clean)
    except ValueError:
        parts = clean.split()
    head = parts[0].lower()
    if head == "approve" and len(parts) >= 2:
        return "approve", [parts[1]]
    if head == "decline" and len(parts) >= 2:
        return "decline", [parts[1], " ".join(parts[2:]).strip()]
    if head in ("pending", "queue", "approvals") and len(parts) == 1:
        return "pending", []
    return "ask", [clean]


def handle_text(conn: psycopg.Connection, tenant_id: str, text: str, *, user: str = "slack") -> str:
    cmd, args = parse_command(text)
    if cmd == "approve":
        aid = args[0]
        try:
            approvals.decide(conn, aid, Decision(decision="approve", decided_by=user))
        except KeyError:
            return f"No approval with id `{aid}`."
        except ValueError as exc:
            return str(exc)
        row = executors.run_approved(conn, aid)
        result = row.get("result") or {}
        if row["status"] == "executed":
            url = f" {result['url']}" if result.get("url") else ""
            return f"Approved and executed `{aid}`: {result.get('text', 'done')}{url}"
        return f"Approved `{aid}` but execution failed: {result.get('error', 'unknown error')}"
    if cmd == "decline":
        aid, reason = args
        try:
            approvals.decide(conn, aid, Decision(decision="decline", reason=reason or None, decided_by=user))
        except KeyError:
            return f"No approval with id `{aid}`."
        except ValueError as exc:
            return str(exc)
        return f"Declined `{aid}`" + (f" — {reason}" if reason else "") + ". Recorded in decisions.md."
    if cmd == "pending":
        rows = approvals.list_pending(conn, tenant_id)
        if not rows:
            return "Nothing waiting for you."
        lines = [f"{len(rows)} waiting:"]
        lines += [f"• `{a.id}` · {a.type} · {a.target}" for a in rows[:20]]
        lines.append("Reply `approve <id>` or `decline <id> <reason>`.")
        return "\n".join(lines)
    if re.search(r"what should i worry about|chief of staff|cos brief", text, re.I):
        from daemon.skills.cockpit import chief_of_staff as cos

        out = cos.run(build_ctx(conn, tenant_id, trigger="ask", question=text))
        risks = out.get("risks") or []
        lines = [out.get("brief") or "No brief."]
        lines += [f"• [{r.get('module')}] {r.get('title')} — {r.get('severity')}" for r in risks[:5]]
        return "\n".join(lines)
    ctx = build_ctx(conn, tenant_id, question=args[0])
    return ask_answer.answer(ctx, args[0])


def _tokens() -> tuple[str | None, str | None]:
    return settings.secret("env:SLACK_APP_TOKEN"), settings.secret("env:SLACK_BOT_TOKEN")


def available() -> bool:
    app, bot = _tokens()
    return bool(app and bot)


def reply_token(conn: psycopg.Connection, tenant_id: str, *, operator_token: str | None) -> str | None:
    """The token a Socket Mode reply must post with: the RESOLVED tenant's own Slack credential.

    The Socket Mode client holds one process-wide operator token. Posting a reply for tenant B with it would
    post as the operator on B's behalf, so the operator token is a fallback for the install tenant only (the
    single-operator dev install that never ran onboarding). No credential → None, and the reply is dropped.
    """
    try:
        token = secrets.credential_for_source(conn, tenant_id, "slack")
    except Exception as exc:  # a missing master key is "no credential", never a crash in the listener
        log.warning("no usable Slack credential for %s: %s", tenant_id, type(exc).__name__)
        token = None
    if token:
        return token
    if tenant_id == settings.tenant_id:
        return operator_token
    return None


def _new_web_client(token: str) -> Any:
    """A WebClient bound to one tenant's token. A seam: tests replace it instead of reaching Slack."""
    from slack_sdk import WebClient

    return WebClient(token=token)


def _reply_client(web: Any, operator_token: str | None, token: str) -> Any:
    """The client to reply with — the gateway's own when the token is the operator's, else a per-tenant one."""
    return web if token == operator_token else _new_web_client(token)


def _listener(web: Any, bot_token: str | None, dev_tenant: str | None) -> Any:
    """The Socket Mode events_api listener. Module level so it is testable without a socket."""

    def on_request(sm: Any, req: Any) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse

        if req.type != "events_api":
            return
        sm.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        event = (req.payload or {}).get("event") or {}
        if event.get("type") not in ("message", "app_mention") or event.get("bot_id") or event.get("subtype"):
            return
        text = event.get("text") or ""
        channel = event.get("channel")
        slack_user = event.get("user") or ""
        user_tenant: str | None = None
        token: str | None = None
        try:
            routed = resolve_event_tenant(slack_user, dev_tenant=dev_tenant)
            if not routed:
                web.chat_postEphemeral(channel=channel, user=slack_user, text=link_hint())
                return
            user_tenant, user_id = routed
            with get_conn(_dsn(), tenant_id=user_tenant) as conn:
                token = reply_token(conn, user_tenant, operator_token=bot_token)
                reply = handle_text(conn, user_tenant, text, user=user_id)
        except Exception as exc:
            log.exception("gateway error: %s", type(exc).__name__)
            reply = f"Something went wrong ({type(exc).__name__}). It's in the run ledger."
        if token is None and user_tenant is None:
            token = bot_token  # nothing resolved: the error line goes out on the operator's own connection
        if not token:
            log.warning("no Slack credential for tenant %s: the Socket Mode reply was not posted", user_tenant)
            return
        _reply_client(web, bot_token, token).chat_postMessage(
            channel=channel, text=reply, thread_ts=event.get("thread_ts")
        )

    return on_request


def start(tenant_id: str | None = None) -> Any | None:
    """Connect Socket Mode and return the client, or None when this is not a dev process or tokens are missing.

    Socket Mode is dev-only: production Slack is served over HTTP (`POST /slack/events`). `tenant_id` overrides
    the fallback tenant for unmapped users, which is a dev-only concept too.
    """
    if not dev_mode():
        log.info(
            "slack gateway not started: Slack is served over HTTP now (POST /slack/events → tenant_jobs → "
            "run_slack_event); Socket Mode is dev-only (STARTUPOS_DEV=1)"
        )
        return None
    app_token, bot_token = _tokens()
    if not (app_token and bot_token):
        log.info("slack gateway disabled: SLACK_APP_TOKEN/SLACK_BOT_TOKEN not set")
        return None
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient

    dev_tenant = tenant_id or settings.tenant_id
    web = WebClient(token=bot_token)
    client = SocketModeClient(app_token=app_token, web_client=web)
    client.socket_mode_request_listeners.append(_listener(web, bot_token, dev_tenant))
    client.connect()
    log.info("slack gateway connected (socket mode, dev)")
    return client
