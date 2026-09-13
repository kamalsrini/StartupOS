"""Slack app install — OAuth v2 (Sprint 3b, Track I).

One Slack app serves the whole install (`STARTUPOS_SLACK_CLIENT_ID/SECRET`, `STARTUPOS_SLACK_SIGNING_SECRET`);
each tenant installs it into its own workspace and gets its own bot token. The token is NEVER an environment
variable and never lands in `slack_installations`:

    oauth.v2.access  →  tenant_secrets['slack_bot_token']            (envelope-encrypted, common/secrets.py)
                     →  connections[slack].secret_ref = 'kv:slack_bot_token'
                     →  slack_installations row (tenant ↔ workspace, no token)

so Track D's delivery and every executor resolve it through `common.secrets.credential_for_source` unchanged.

One Slack workspace maps to exactly one tenant (`slack_installations.team_id` is UNIQUE). A second tenant
installing into a workspace that is already connected is refused cleanly (`WorkspaceTaken`) instead of
hijacking the first tenant's channel — the callback turns that into `?slack=error&reason=workspace_taken`.

Nothing in this module logs a code, a token, or the client secret.
"""

from __future__ import annotations

import json
import logging
import secrets as pysecrets
from typing import Any
from urllib.parse import urlencode

import httpx
import psycopg
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from auth import config
from common import secrets
from common.settings import settings

log = logging.getLogger("auth.slack_install")

AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
ACCESS_URL = "https://slack.com/api/oauth.v2.access"
# CONTRACTS.md Sprint 3b Track I. chat:write.public lets the bot post in a public channel it was not invited to.
SCOPES: tuple[str, ...] = (
    "chat:write",
    "chat:write.public",
    "commands",
    "im:history",
    "app_mentions:read",
    "users:read",
    "channels:read",
)
STATE_SALT = "sos_slack_install"
STATE_MAX_AGE = 600  # 10 minutes — one install attempt
SECRET_NAME = "slack_bot_token"  # noqa: S105 - the NAME of the tenant_secrets row, not a value
SECRET_REF = f"kv:{SECRET_NAME}"
SOURCE = "slack"
DEFAULT_CHANNEL = "#general"

_http = httpx.Client(timeout=10.0)


class SlackInstallError(Exception):
    """Any failure of the install flow. Messages are reasons, never values."""


class SlackNotConfigured(SlackInstallError):
    """STARTUPOS_SLACK_CLIENT_ID/SECRET are not set: the install routes answer 503."""


class WorkspaceTaken(SlackInstallError):
    """This Slack workspace is already installed for another tenant (team_id is UNIQUE)."""


# --- configuration ----------------------------------------------------------------


def configured() -> bool:
    return bool(settings.slack_client_id() and settings.slack_client_secret())


def signing_configured() -> bool:
    return bool(settings.slack_signing_secret())


def warn_if_unconfigured(logger: logging.Logger | None = None) -> bool:
    """One startup line per service (api/daemon). Absent Slack env is a degraded install, not a failure."""
    logger = logger or log
    missing = [
        name
        for name, value in (
            ("STARTUPOS_SLACK_CLIENT_ID", settings.slack_client_id()),
            ("STARTUPOS_SLACK_CLIENT_SECRET", settings.slack_client_secret()),
            ("STARTUPOS_SLACK_SIGNING_SECRET", settings.slack_signing_secret()),
        )
        if not value
    ]
    if missing:
        logger.warning(
            "Slack app is not configured (%s): GET /slack/install and the inbound Slack routes answer 503; "
            "everything else runs normally.",
            ", ".join(missing),
        )
        return False
    return True


def redirect_uri() -> str:
    return config.public_url() + "/slack/oauth/callback"


# --- state token -------------------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.session_secret(), salt=STATE_SALT)


def make_state(tenant_id: str, user_id: str | None = None) -> str:
    """Signed, timestamped state carrying the tenant + a nonce. Slack hands it back on the callback."""
    return _serializer().dumps({"t": tenant_id, "u": user_id, "n": pysecrets.token_urlsafe(16)})


def read_state(value: str | None, *, max_age: int = STATE_MAX_AGE) -> dict[str, Any] | None:
    """{'tenant_id', 'user_id'} or None when missing, tampered, or older than `max_age`."""
    if not value:
        return None
    try:
        data = _serializer().loads(value, max_age=max_age)
    except (BadSignature, SignatureExpired, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("t"):
        return None
    return {"tenant_id": str(data["t"]), "user_id": data.get("u")}


def authorize_url(state: str) -> str:
    client_id = settings.slack_client_id()
    if not client_id:
        raise SlackNotConfigured("STARTUPOS_SLACK_CLIENT_ID is not configured")
    params = {
        "client_id": client_id,
        "scope": ",".join(SCOPES),
        "redirect_uri": redirect_uri(),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


# --- code exchange -----------------------------------------------------------------


def exchange_code(code: str, *, client: Any | None = None) -> dict[str, Any]:
    """Authorization code → oauth.v2.access response. Raises SlackInstallError; never logs the code or token.

    `client` is any object with `.post(url, data=…)` returning a response with `.json()` — the tests inject a
    fake so no test ever reaches the network.
    """
    client_id, client_secret = settings.slack_client_id(), settings.slack_client_secret()
    if not (client_id and client_secret):
        raise SlackNotConfigured("STARTUPOS_SLACK_CLIENT_ID/STARTUPOS_SLACK_CLIENT_SECRET are not configured")
    try:
        resp = (client or _http).post(
            ACCESS_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri(),
            },
        )
    except httpx.HTTPError as exc:
        raise SlackInstallError(f"slack oauth exchange failed: {type(exc).__name__}") from None
    status = getattr(resp, "status_code", 200)
    if status != 200:
        raise SlackInstallError(f"slack oauth exchange failed: HTTP {status}")
    try:
        body = resp.json()
    except Exception:
        raise SlackInstallError("slack oauth exchange returned a non-JSON body") from None
    if not isinstance(body, dict) or not body.get("ok"):
        reason = str((body or {}).get("error") or "unknown_error") if isinstance(body, dict) else "unknown_error"
        raise SlackInstallError(f"slack oauth exchange refused: {reason}")
    if not body.get("access_token"):
        raise SlackInstallError("slack oauth response has no bot token")
    team = body.get("team") or {}
    if not team.get("id"):
        raise SlackInstallError("slack oauth response has no team id")
    return body


# --- installation rows --------------------------------------------------------------


INSTALL_COLUMNS = "tenant_id, team_id, team_name, bot_user_id, default_channel, installed_by, installed_at, revoked_at"


def lookup_team(conn: psycopg.Connection, team_id: str) -> dict[str, Any] | None:
    """team_id → installation, with no tenant bound (SECURITY DEFINER `slack_lookup_install`, db/rls.sql).

    This is the ONLY tenant resolution for an inbound Slack request: never a header, never a query parameter.
    A revoked install resolves to a row with `revoked_at` set — callers treat that as "not connected".
    """
    if not team_id:
        return None
    row = conn.execute("SELECT * FROM slack_lookup_install(%s)", (team_id,)).fetchone()
    return dict(row) if row else None


def get_installation(conn: psycopg.Connection, tenant_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {INSTALL_COLUMNS} FROM slack_installations WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()
    return dict(row) if row else None


def save_installation(
    conn: psycopg.Connection,
    tenant_id: str,
    oauth: dict[str, Any],
    *,
    installed_by: str | None = None,
    anon_conn: psycopg.Connection | None = None,
) -> dict[str, Any]:
    """Store the bot token, the `connections` row and the installation, in that order, on a tenant-bound conn.

    Raises WorkspaceTaken when the workspace already belongs to another live tenant (team_id is UNIQUE) —
    checked through the cross-tenant lookup first and again by the unique index, so a race is still refused.
    """
    team = oauth.get("team") or {}
    team_id = str(team.get("id") or "")
    if not team_id:
        raise SlackInstallError("slack oauth response has no team id")
    existing = lookup_team(anon_conn or conn, team_id)
    if existing and existing["tenant_id"] != tenant_id:
        raise WorkspaceTaken(f"workspace {team_id} is already connected to another StartupOS company")

    secrets.put(conn, tenant_id, SECRET_NAME, str(oauth["access_token"]))
    bot_user_id = str(oauth.get("bot_user_id") or "") or None
    channel = _incoming_channel(oauth) or DEFAULT_CHANNEL
    conn.execute(
        """INSERT INTO connections (id, tenant_id, source, secret_ref, config, status)
           VALUES (%s, %s, %s, %s, %s::jsonb, 'connected')
           ON CONFLICT (tenant_id, source) DO UPDATE
             SET secret_ref = EXCLUDED.secret_ref,
                 config = connections.config || EXCLUDED.config,
                 status = 'connected',
                 last_error = NULL""",
        (
            f"{tenant_id}:{SOURCE}",
            tenant_id,
            SOURCE,
            SECRET_REF,
            json.dumps({"team_id": team_id, "default_channel": channel}),
        ),
    )
    try:
        row = conn.execute(
            f"""INSERT INTO slack_installations
                  (tenant_id, team_id, team_name, bot_user_id, default_channel, installed_by, installed_at, revoked_at)
                VALUES (%s, %s, %s, %s, %s, %s, now(), NULL)
                ON CONFLICT (tenant_id) DO UPDATE
                  SET team_id = EXCLUDED.team_id, team_name = EXCLUDED.team_name,
                      bot_user_id = EXCLUDED.bot_user_id, default_channel = EXCLUDED.default_channel,
                      installed_by = EXCLUDED.installed_by, installed_at = now(), revoked_at = NULL
                RETURNING {INSTALL_COLUMNS}""",
            (tenant_id, team_id, team.get("name"), bot_user_id, channel, installed_by),
        ).fetchone()
    except psycopg.errors.UniqueViolation:
        raise WorkspaceTaken(f"workspace {team_id} is already connected to another StartupOS company") from None
    return dict(row)


def _incoming_channel(oauth: dict[str, Any]) -> str | None:
    """Slack returns the chosen channel when the `incoming-webhook` scope is granted; we use it as the default."""
    hook = oauth.get("incoming_webhook") or {}
    channel = hook.get("channel")
    return str(channel) if channel else None


def revoke(conn: psycopg.Connection, tenant_id: str) -> dict[str, Any]:
    """Uninstall: mark the installation revoked, delete the bot token, disable the `connections` row.

    All three, always — a revoked install that kept its token would keep posting until the token expired.
    """
    cur = conn.execute(
        "UPDATE slack_installations SET revoked_at = now() WHERE tenant_id = %s AND revoked_at IS NULL",
        (tenant_id,),
    )
    deleted = secrets.delete(conn, tenant_id, SECRET_NAME)
    disabled = conn.execute(
        "UPDATE connections SET status = 'disabled' WHERE tenant_id = %s AND source = %s", (tenant_id, SOURCE)
    )
    return {
        "revoked": cur.rowcount > 0,
        "secret_deleted": deleted,
        "connection_disabled": disabled.rowcount > 0,
    }
