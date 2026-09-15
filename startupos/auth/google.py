"""Google OIDC (Authorization Code flow). Discovery + JWKS cached in-process; network calls are injectable.

Nothing here logs codes, tokens or id_tokens.
"""

from __future__ import annotations

import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from itsdangerous import BadSignature, URLSafeTimedSerializer

from auth import config

DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
VALID_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
OAUTH_COOKIE = "sos_oauth"
OAUTH_COOKIE_MAX_AGE = 600  # seconds — one sign-in attempt
# Sprint 3d (Track G): a verified Google identity that matches no user is offered a company of their own. The
# id_token that proved that identity is handed to POST /onboarding/tenant in this short-lived, signed, HttpOnly
# cookie instead of being put in a redirect URL — a URL would land in browser history, the Referer header and
# any proxy log, and JavaScript would be able to read it.
SIGNUP_COOKIE = "sos_signup"
SIGNUP_COOKIE_MAX_AGE = 900  # seconds — long enough to type a company name, shorter than the id_token's hour
JWKS_TTL = 3600
DISCOVERY_TTL = 24 * 3600

_http = httpx.Client(timeout=10.0)
_discovery_cache: tuple[float, dict[str, Any]] | None = None
_jwks_cache: tuple[float, dict[str, Any]] | None = None


class GoogleAuthError(Exception):
    """Any failure to authenticate with Google (config, network, or an invalid id_token)."""


class UnverifiedEmailError(GoogleAuthError):
    """Google authenticated the account but has not verified the address (email_verified is not true).

    Its own class because it is the one failure with a useful answer for the person: it is not a bug, a stale
    cookie or a forgery — Google will not vouch for that address, so neither can we.
    """


def configured() -> bool:
    return bool(config.google_client_id())


def redirect_uri() -> str:
    return config.public_url() + "/auth/google/callback"


# --- discovery / JWKS ---------------------------------------------------------


def discovery(client: httpx.Client | None = None) -> dict[str, Any]:
    global _discovery_cache
    now = time.time()
    if _discovery_cache and now - _discovery_cache[0] < DISCOVERY_TTL:
        return _discovery_cache[1]
    r = (client or _http).get(DISCOVERY_URL)
    r.raise_for_status()
    doc = r.json()
    _discovery_cache = (now, doc)
    return doc


def jwks(client: httpx.Client | None = None, *, force: bool = False) -> dict[str, Any]:
    """Google's signing keys, cached for JWKS_TTL. `force` refetches (unknown kid → rotated keys)."""
    global _jwks_cache
    now = time.time()
    if not force and _jwks_cache and now - _jwks_cache[0] < JWKS_TTL:
        return _jwks_cache[1]
    uri = discovery(client).get("jwks_uri") or "https://www.googleapis.com/oauth2/v3/certs"
    r = (client or _http).get(uri)
    r.raise_for_status()
    keys = r.json()
    _jwks_cache = (now, keys)
    return keys


def reset_caches() -> None:
    global _discovery_cache, _jwks_cache
    _discovery_cache = None
    _jwks_cache = None


# --- state / nonce cookie ---------------------------------------------------------


def _oauth_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.session_secret(), salt=OAUTH_COOKIE)


def new_state_and_nonce() -> tuple[str, str]:
    return secrets.token_urlsafe(24), secrets.token_urlsafe(24)


def make_oauth_cookie(state: str, nonce: str) -> str:
    return _oauth_serializer().dumps({"state": state, "nonce": nonce})


def read_oauth_cookie(value: str | None) -> tuple[str, str] | None:
    if not value:
        return None
    try:
        data = _oauth_serializer().loads(value, max_age=OAUTH_COOKIE_MAX_AGE)
    except (BadSignature, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("state") or not data.get("nonce"):
        return None
    return str(data["state"]), str(data["nonce"])


def _signup_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.session_secret(), salt=SIGNUP_COOKIE)


def make_signup_cookie(id_token: str) -> str:
    return _signup_serializer().dumps({"id_token": id_token})


def read_signup_cookie(value: str | None) -> str | None:
    """The id_token carried by a sign-up cookie, or None (absent, forged, or older than SIGNUP_COOKIE_MAX_AGE).

    The id_token is still verified by the caller: this cookie only carries it, it never vouches for it.
    """
    if not value:
        return None
    try:
        data = _signup_serializer().loads(value, max_age=SIGNUP_COOKIE_MAX_AGE)
    except (BadSignature, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("id_token"):
        return None
    return str(data["id_token"])


# --- flow ------------------------------------------------------------------------


def build_auth_url(state: str, nonce: str, client: httpx.Client | None = None) -> str:
    client_id = config.google_client_id()
    if not client_id:
        raise GoogleAuthError("GOOGLE_CLIENT_ID is not configured")
    endpoint = discovery(client).get("authorization_endpoint") or "https://accounts.google.com/o/oauth2/v2/auth"
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri(),
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    return f"{endpoint}?{urlencode(params)}"


def exchange_code(code: str, client: httpx.Client | None = None) -> dict[str, Any]:
    """Authorization code → token response (contains id_token). Raises GoogleAuthError on failure."""
    client_id, client_secret = config.google_client_id(), config.google_client_secret()
    if not (client_id and client_secret):
        raise GoogleAuthError("GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are not configured")
    endpoint = discovery(client).get("token_endpoint") or "https://oauth2.googleapis.com/token"
    try:
        r = (client or _http).post(
            endpoint,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
    except httpx.HTTPError as exc:
        raise GoogleAuthError(f"token exchange failed: {type(exc).__name__}") from exc
    if r.status_code != 200:
        raise GoogleAuthError(f"token exchange failed: HTTP {r.status_code}")
    body = r.json()
    if not body.get("id_token"):
        raise GoogleAuthError("token response has no id_token")
    return body


def _key_for(token: str, client: httpx.Client | None) -> Any:
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise GoogleAuthError("malformed id_token") from exc
    kid = header.get("kid")
    for attempt in (False, True):
        for k in jwks(client, force=attempt).get("keys", []):
            if k.get("kid") == kid:
                return jwt.PyJWK(k).key
    raise GoogleAuthError("no matching signing key")


def verify_id_token(
    id_token: str, nonce: str | None = None, client: httpx.Client | None = None, *, audience: str | None = None
) -> dict[str, Any]:
    """Verify signature, iss, aud, exp, nonce (when given) and email_verified. Returns the claims."""
    aud = audience or config.google_client_id()
    if not aud:
        raise GoogleAuthError("GOOGLE_CLIENT_ID is not configured")
    key = _key_for(id_token, client)
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=aud,
            options={"require": ["exp", "iat", "sub", "aud", "iss"], "verify_iss": False},
        )
    except jwt.PyJWTError as exc:
        raise GoogleAuthError(f"invalid id_token: {type(exc).__name__}") from exc
    if claims.get("iss") not in VALID_ISSUERS:
        raise GoogleAuthError("invalid id_token: issuer")
    if nonce is not None and claims.get("nonce") != nonce:
        raise GoogleAuthError("invalid id_token: nonce")
    if not claims.get("email"):
        raise GoogleAuthError("invalid id_token: no email")
    if claims.get("email_verified") is not True:
        raise UnverifiedEmailError("invalid id_token: email not verified")
    return claims
