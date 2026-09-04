"""Slack gateway (Socket Mode). Commands in DM or @mention:

    approve <id>              → approvals.decide(approve) and execute immediately
    decline <id> <reason>     → approvals.decide(decline, reason)
    pending                   → list pending approvals
    anything else             → ask.answer

Starts only when SLACK_APP_TOKEN and SLACK_BOT_TOKEN exist. `handle_text` is pure and testable.
Tokens are read via common.settings and never logged.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import Any

import psycopg

from common.db import get_conn
from common.models import Decision
from common.settings import settings
from daemon import approvals, executors
from daemon.skills import build_ctx
from daemon.skills.ask import answer as ask_answer

log = logging.getLogger("daemon.gateway.slack")
_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")


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
    ctx = build_ctx(conn, tenant_id, question=args[0])
    return ask_answer.answer(ctx, args[0])


def _tokens() -> tuple[str | None, str | None]:
    return settings.secret("env:SLACK_APP_TOKEN"), settings.secret("env:SLACK_BOT_TOKEN")


def available() -> bool:
    app, bot = _tokens()
    return bool(app and bot)


def start(tenant_id: str | None = None) -> Any | None:
    """Connect Socket Mode and return the client, or None when tokens are missing."""
    app_token, bot_token = _tokens()
    if not (app_token and bot_token):
        log.info("slack gateway disabled: SLACK_APP_TOKEN/SLACK_BOT_TOKEN not set")
        return None
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse

    tenant_id = tenant_id or settings.tenant_id
    web = WebClient(token=bot_token)
    client = SocketModeClient(app_token=app_token, web_client=web)

    def on_request(sm: SocketModeClient, req: SocketModeRequest) -> None:
        if req.type != "events_api":
            return
        sm.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        event = (req.payload or {}).get("event") or {}
        if event.get("type") not in ("message", "app_mention") or event.get("bot_id") or event.get("subtype"):
            return
        text = event.get("text") or ""
        channel = event.get("channel")
        user = event.get("user") or "slack"
        try:
            with get_conn() as conn:
                reply = handle_text(conn, tenant_id, text, user=user)
        except Exception as exc:
            log.exception("gateway error: %s", type(exc).__name__)
            reply = f"Something went wrong ({type(exc).__name__}). It's in the run ledger."
        web.chat_postMessage(channel=channel, text=reply, thread_ts=event.get("thread_ts"))

    client.socket_mode_request_listeners.append(on_request)
    client.connect()
    log.info("slack gateway connected (socket mode)")
    return client
