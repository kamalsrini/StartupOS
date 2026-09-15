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


# --- public path prefix -------------------------------------------------------------
#
# Sprint 3d topology: one hostname, one certificate. `https://<domain>/` is the Next.js app and
# `https://<domain>/api/...` is this API; the proxy passes `/api/...` through UNCHANGED (it does not strip the
# prefix), so the API has to own it. STARTUPOS_PATH_PREFIX carries that prefix.
#
# Empty (the default, and what local dev and CI use) means "mount everything at the root", byte-for-byte the
# behaviour that shipped before this existed. Non-empty means every route AND every Set-Cookie Path move under
# the prefix — a cookie written with Path=/auth/google would never be sent back to /api/auth/google/callback,
# which is exactly how an OAuth round trip dies silently.


def path_prefix() -> str:
    """Mount prefix for every API route. '' (default) or a normalised '/api'-shaped prefix."""
    raw = (_get("path_prefix", "STARTUPOS_PATH_PREFIX", "") or "").strip()
    if not raw or raw == "/":
        return ""
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/")


def prefixed(path: str) -> str:
    """Absolute route path under the mount prefix: '/health' -> '/health' or '/api/health'.

    The API root keeps its trailing slash ('/' -> '/api/') so the proxy's `/api/` lands on it.
    """
    prefix = path_prefix()
    if not prefix:
        return path
    if path in ("", "/"):
        return prefix + "/"
    return prefix + (path if path.startswith("/") else "/" + path)


def cookie_path(path: str = "/") -> str:
    """Set-Cookie Path under the mount prefix: cookie_path('/auth/google') -> '/api/auth/google'.

    Use this for EVERY cookie the API writes or deletes. A set and its matching delete must agree, or the
    browser keeps the stale cookie. '/' maps to the bare prefix ('/api'), which RFC 6265 path-matches
    '/api' and everything under '/api/'.
    """
    prefix = path_prefix()
    if not prefix:
        return path
    if path in ("", "/"):
        return prefix
    return prefix + (path if path.startswith("/") else "/" + path)


def public_url_prefix_mismatch() -> str | None:
    """Warning text when STARTUPOS_PUBLIC_URL does not end with STARTUPOS_PATH_PREFIX, else None.

    STARTUPOS_PUBLIC_URL is the full public base INCLUDING the prefix (https://<domain>/api), because it is what
    auth.google.redirect_uri() and the Slack redirect/request URLs are built from. Getting these two out of step
    produces a redirect_uri Google rejects, so we say so loudly at startup instead of at first sign-in.
    """
    prefix = path_prefix()
    if not prefix:
        return None
    base = public_url()
    if base.endswith(prefix):
        return None
    return (
        f"STARTUPOS_PATH_PREFIX={prefix} but STARTUPOS_PUBLIC_URL={base!r} does not end with it. "
        f"Set STARTUPOS_PUBLIC_URL to the full public base including the prefix (e.g. https://<domain>{prefix}), "
        "or Google/Slack redirect URIs will not match what the proxy serves."
    )


# --- self-serve sign-up and the public deployment ------------------------------------
#
# Sprint 3d shipped one public URL and self-serve Google sign-up: any stranger who has the link arrives with a
# tenant of their own, which the daemon then spends tokens for. Rate limiting caps the RATE of that; these two
# switches cap the rest. The default is the open behaviour Track G shipped, because sharing the link is the
# whole point — but an operator who wakes up to a bill needs one variable, not a redeploy of new code.


def allow_signup() -> bool:
    """May a stranger create a new tenant? STARTUPOS_ALLOW_SIGNUP=0 closes it. Default: open (Track G)."""
    raw = _get("allow_signup", "STARTUPOS_ALLOW_SIGNUP", "1")
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def public_deployment() -> bool:
    """Is this the deployment that is on the internet?

    Keyed off STARTUPOS_DOMAIN / STARTUPOS_PATH_PREFIX — the two variables a public deploy CANNOT work without
    (docs/HOSTING.md makes the operator set both together). A dedicated "is this production" flag would be a
    variable someone can forget; these cannot be forgotten, because nothing is public until they are set.
    """
    domain = (os.environ.get("STARTUPOS_DOMAIN") or "").strip()
    return bool(domain) or bool(path_prefix())


def docs_enabled() -> bool:
    """Serve /docs, /redoc and /openapi.json? Off on a public deployment, on in dev.

    Interactive API documentation on a shared host is a map of every route, parameter and schema handed to
    whoever finds the URL. It carries no tenant data, so this is not a breach — it is reconnaissance, and it is
    one line to switch off. STARTUPOS_ENABLE_DOCS=1 forces them back on (an operator debugging a deploy);
    STARTUPOS_ENABLE_DOCS=0 switches them off locally too.
    """
    raw = os.environ.get("STARTUPOS_ENABLE_DOCS")
    if raw not in (None, ""):
        return str(raw).strip().lower() not in ("0", "false", "no", "off")
    return not public_deployment()
