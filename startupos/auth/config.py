"""Auth settings. Read lazily (per call) so tests can monkeypatch the environment.

Prefers the attribute on `common.settings.settings` when the PE has added it; falls back to the env var of the
same name (see NEEDS_PE.md). Values are never logged.
"""

from __future__ import annotations

import logging
import os

from common.settings import settings

log = logging.getLogger("auth.config")

DEV_INSECURE_SECRET = "startupos-dev-insecure-session-secret"  # noqa: S105 - documented dev-only fallback
_warned_dev_secret = False


def _get(attr: str, env: str, default: str | None = None) -> str | None:
    v = os.environ.get(env)
    if v not in (None, ""):
        return v
    v = getattr(settings, attr, None)
    return v if v not in (None, "") else default


def dev_mode() -> bool:
    return os.environ.get("STARTUPOS_DEV") == "1"


def session_secret() -> str:
    """HMAC secret for the session cookie. Required unless STARTUPOS_DEV=1 (then a fixed insecure secret + warning)."""
    global _warned_dev_secret
    secret = _get("session_secret", "STARTUPOS_SESSION_SECRET")
    if secret:
        return secret
    if dev_mode():
        if not _warned_dev_secret:
            log.warning("STARTUPOS_SESSION_SECRET not set; using the insecure dev secret because STARTUPOS_DEV=1")
            _warned_dev_secret = True
        return DEV_INSECURE_SECRET
    raise RuntimeError(
        "STARTUPOS_SESSION_SECRET is not set. Set it (e.g. `openssl rand -hex 32`) or export STARTUPOS_DEV=1 "
        "for a local insecure fallback."
    )


def session_ttl_hours() -> int:
    raw = _get("session_ttl_hours", "STARTUPOS_SESSION_TTL_HOURS", "336")
    try:
        return max(1, int(raw or "336"))
    except ValueError:
        return 336


def google_client_id() -> str | None:
    return _get("google_client_id", "GOOGLE_CLIENT_ID")


def google_client_secret() -> str | None:
    return _get("google_client_secret", "GOOGLE_CLIENT_SECRET")


def bootstrap_token() -> str | None:
    return _get("bootstrap_token", "STARTUPOS_BOOTSTRAP_TOKEN")


def public_url() -> str:
    return (_get("public_url", "STARTUPOS_PUBLIC_URL", "http://localhost:8000") or "").rstrip("/")


def web_url() -> str:
    return (_get("web_url", "STARTUPOS_WEB_URL", "http://localhost:3000") or "").rstrip("/")


def cookie_secure() -> bool:
    """Default True; STARTUPOS_COOKIE_SECURE=0 disables (the API also relaxes it for localhost hosts)."""
    raw = _get("cookie_secure", "STARTUPOS_COOKIE_SECURE", "1")
    return str(raw).lower() not in ("0", "false", "no")


def app_dsn() -> str:
    return _get("app_dsn", "STARTUPOS_APP_DSN") or settings.database_url
