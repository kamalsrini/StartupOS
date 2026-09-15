"""Auth endpoints: Google OIDC, bootstrap, logout, me, API tokens, Slack link.

Public: GET /auth/google, GET /auth/google/callback, POST /auth/bootstrap. Everything else needs a principal.
Never logs or returns tokens, cookies, id_tokens, secrets or token hashes.
"""

from __future__ import annotations

import hmac
import html
from typing import Any
from urllib.parse import urlencode

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from api import ratelimit
from api.deps import (
    api_dsn,
    clear_session_cookie,
    cookie_secure_for,
    current_principal,
    get_db,
    optional_principal,
    set_session_cookie,
)
from auth import bootstrap, config, google, sessions, tokens
from auth.identity import Principal
from common.db import get_conn
from common.settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])


class BootstrapIn(BaseModel):
    token: str
    email: str = Field(min_length=3, max_length=200)
    name: str | None = Field(default=None, max_length=120)


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class SlackLinkIn(BaseModel):
    slack_user_id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Z0-9]+$")


def _web(path: str) -> str:
    return config.web_url() + path


# --- Google OIDC -------------------------------------------------------------------
#
# Sprint 3d (Track G). Two rules decide what happens after Google has vouched for an address:
#
#   * the address already has a user  -> sign that person in, in THEIR tenant (never settings.tenant_id);
#   * the address has no user at all  -> offer them a NEW company of their own (POST /onboarding/tenant with
#     the verified id_token), because the whole point of one public URL is that a founder can send it to
#     someone and they can get in.
#
# Joining a company that already exists stays invite-only: there is no "request to join" here, and a user row
# that exists but is not active is refused (403) rather than quietly given a second, empty workspace.
#
# The callback is only ever reached by a browser, so its failures answer with a small HTML page that says what
# went wrong and links back to the app, instead of a bare JSON body on an API URL that a person cannot act on.
# The status codes are unchanged (403 / 503).


def _login_url(**params: str) -> str:
    qs = urlencode({k: v for k, v in params.items() if v})
    return _web("/login") + (f"?{qs}" if qs else "")


SIGN_IN_PROBLEMS: dict[str, tuple[int, str, str]] = {
    "cancelled": (
        403,
        "Sign-in cancelled",
        "You closed Google's sign-in window, or declined. Nothing was changed. You can try again.",
    ),
    "state_mismatch": (
        403,
        "This sign-in link has expired",
        "Sign-in has to start on the StartupOS sign-in page, and has to finish within 10 minutes. This one did "
        "not, so we stopped it rather than trust it. Start again — it usually works the second time.",
    ),
    "email_unverified": (
        403,
        "Google has not verified that address",
        "Google signed you in but has not confirmed that this e-mail address is yours, so we cannot use it to "
        "identify you. Verify the address with Google (or sign in with a different Google account) and try again.",
    ),
    "failed": (
        403,
        "Google sign-in did not complete",
        "Google did not return a usable answer. Nothing was changed and you are not signed in. Please try again.",
    ),
    "account_inactive": (
        403,
        "That account is not active",
        "This e-mail address belongs to a company on StartupOS, but the account is not active — it has been "
        "disabled, or the invitation was never completed. Ask that company's owner to re-invite you. We will "
        "not create a second, empty company for an address that already belongs to one.",
    ),
    "signup_closed": (
        403,
        "New companies are closed here",
        "Google signed you in, but this StartupOS installation is not accepting new companies right now, so we "
        "did not create one. Nothing was changed. If you were invited to a company that is already here, ask "
        "its owner to invite this exact address and then sign in; otherwise ask whoever runs this installation.",
    ),
    "not_configured": (
        503,
        "Google sign-in is not configured",
        "This installation has no Google OAuth client (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET). "
        "The operator has to create one — see docs/GOOGLE-SIGNIN.md.",
    ),
}


def _sign_in_problem(request: Request, reason: str) -> Response:
    """A human-readable dead end for the browser, a JSON detail for anything that asked for JSON."""
    status, headline, detail = SIGN_IN_PROBLEMS[reason]
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(status_code=status, content={"detail": detail, "reason": reason})
    back = html.escape(_login_url(error=reason), quote=True)
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(headline)} — StartupOS</title>
<style>body{{font:14px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
background:#fafaf9;color:#1c1917;margin:0;display:flex;min-height:100vh;align-items:center;
justify-content:center;padding:24px}}
.card{{max-width:29rem;background:#fff;border:1px solid #e7e5e4;border-radius:8px;padding:24px}}
h1{{font-size:16px;margin:0 0 10px}}p{{color:#57534e;margin:0 0 20px}}
a{{display:inline-block;background:#1c1917;color:#fafaf9;text-decoration:none;padding:7px 14px;
border-radius:5px;font-size:12px;font-weight:600}}</style>
</head><body><div class="card"><h1>{html.escape(headline)}</h1><p>{html.escape(detail)}</p>
<a href="{back}">Back to sign-in</a></div></body></html>"""
    return HTMLResponse(content=body, status_code=status)


@router.get("/providers")
def providers() -> dict[str, Any]:
    """What sign-in methods this installation actually has. Public, and deliberately boolean-only.

    The sign-in page asks first, so an unconfigured install says "Google sign-in is not configured" in the UI
    instead of sending the visitor to a 503 on an API URL.
    """
    return {"google": google.configured(), "bootstrap": bootstrap.enabled(), "signup": config.allow_signup()}


@router.get("/google")
def google_start(request: Request) -> Response:
    if not google.configured():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured (GOOGLE_CLIENT_ID)")
    state, nonce = google.new_state_and_nonce()
    try:
        url = google.build_auth_url(state, nonce)
    except google.GoogleAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        google.OAUTH_COOKIE,
        google.make_oauth_cookie(state, nonce),
        max_age=google.OAUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=cookie_secure_for(request),
        path=config.cookie_path("/auth/google"),
    )
    return resp


@router.get("/google/callback")
def google_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> Response:
    if error:
        return _sign_in_problem(request, "cancelled")
    if not google.configured():
        return _sign_in_problem(request, "not_configured")
    saved = google.read_oauth_cookie(request.cookies.get(google.OAUTH_COOKIE))
    # No cookie at all is the same answer as a wrong one: a stale tab, a 10-minute-old link, a third-party
    # cookie policy that dropped it, or someone opening /auth/google/callback by hand.
    # (`state.isascii()` before compare_digest: it raises TypeError on non-ASCII strings, and the state comes
    # from the query string, so a crafted one would be a 500 instead of a refusal.)
    if not saved or not state or not code or not state.isascii() or not hmac.compare_digest(saved[0], state):
        return _sign_in_problem(request, "state_mismatch")
    _, nonce = saved
    try:
        token_resp = google.exchange_code(code)
        id_token = token_resp["id_token"]
        claims = google.verify_id_token(id_token, nonce)
    except google.UnverifiedEmailError:
        return _sign_in_problem(request, "email_unverified")
    except google.GoogleAuthError:
        return _sign_in_problem(request, "failed")

    sub, email = str(claims["sub"]), str(claims["email"]).lower()
    with get_conn(api_dsn(), tenant_id="") as anon:
        hit = anon.execute("SELECT * FROM auth_lookup_google(%s, %s)", (sub, email)).fetchone()

    if not hit:
        if not config.allow_signup():
            # Sign-up closed (STARTUPOS_ALLOW_SIGNUP=0). Say so here rather than issue a sign-up cookie for a
            # POST /onboarding/tenant that will refuse it — a dead end one step later is the thing Track G set
            # out to remove. Signing in as an existing user is untouched.
            return _sign_in_problem(request, "signup_closed")
        # Nobody by this address anywhere: offer them their own company. The verified id_token travels to
        # POST /onboarding/tenant in a signed HttpOnly cookie; the page it lands on says, in as many words,
        # that this creates a NEW empty company and is not how you join a colleague's.
        resp = RedirectResponse(_login_url(new="google", email=email), status_code=302)
        resp.set_cookie(
            google.SIGNUP_COOKIE,
            google.make_signup_cookie(id_token),
            max_age=google.SIGNUP_COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=cookie_secure_for(request),
            path=config.cookie_path("/onboarding"),
        )
        resp.delete_cookie(google.OAUTH_COOKIE, path=config.cookie_path("/auth/google"))
        return resp
    if hit["status"] != "active":
        return _sign_in_problem(request, "account_inactive")

    # The tenant is the one this user belongs to — never the install's own TENANT_ID.
    with get_conn(api_dsn(), tenant_id=hit["tenant_id"]) as conn:
        conn.execute(
            """UPDATE users SET google_sub = COALESCE(google_sub, %s), name = COALESCE(name, %s), last_login_at = now()
                WHERE id = %s""",
            (sub, claims.get("name"), hit["user_id"]),
        )
        _, cookie_value = sessions.create_session(
            conn, hit["tenant_id"], hit["user_id"], request.headers.get("user-agent")
        )
    resp = RedirectResponse(_web("/cockpit"), status_code=302)
    set_session_cookie(resp, request, cookie_value)
    resp.delete_cookie(google.OAUTH_COOKIE, path=config.cookie_path("/auth/google"))
    # A half-finished sign-up from an earlier attempt must not outlive a real sign-in.
    resp.delete_cookie(google.SIGNUP_COOKIE, path=config.cookie_path("/onboarding"))
    return resp


# --- bootstrap -----------------------------------------------------------------


# 5 attempts per source per minute is plenty for a one-time owner bootstrap and makes the high-entropy token
# effectively unguessable online (PE review 2026-09-04). The limiter moved to api/ratelimit.py in Sprint 3d so
# POST /onboarding/tenant — which checks the SAME token — shares it, and so the key is the real client rather
# than Caddy's address (see that module).
_bootstrap_throttle = ratelimit.Throttle(name="auth.bootstrap")


@router.post("/bootstrap")
def bootstrap_owner(body: BootstrapIn, request: Request, response: Response) -> dict[str, Any]:
    """Create/activate the first owner of settings.tenant_id and start a session. 404 when disabled, 403 on bad token."""
    if not bootstrap.enabled():
        raise HTTPException(status_code=404, detail="bootstrap is disabled")
    if not _bootstrap_throttle.allow(ratelimit.client_key(request)):
        raise HTTPException(status_code=429, detail="too many bootstrap attempts; wait a minute")
    if not bootstrap.check_token(body.token):
        raise HTTPException(status_code=403, detail="bad bootstrap token")
    tenant_id = settings.tenant_id
    with get_conn(api_dsn(), tenant_id=tenant_id) as conn:
        user = bootstrap.bootstrap(conn, tenant_id, body.email, body.name)
        _, cookie_value = sessions.create_session(conn, tenant_id, user["id"], request.headers.get("user-agent"))
    set_session_cookie(response, request, cookie_value)
    return {"user": {k: user[k] for k in ("id", "email", "name", "tenant_id", "role")}, "via": "bootstrap"}


# --- session ---------------------------------------------------------------------


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    principal: Principal | None = Depends(optional_principal),
) -> dict[str, Any]:
    """Revoke the session server-side and clear the cookie. Always 200.

    Sprint 3d (Track G): signing out must not need a valid session. It used to 401 when the session had already
    expired or been revoked elsewhere, which left the dead cookie in the browser and the person stuck on a page
    that kept bouncing them to /login — the one state where "Sign out" is what they will press.
    """
    revoked = False
    if principal and principal.session_id:
        with get_conn(api_dsn(), tenant_id=principal.tenant_id) as conn:
            sessions.revoke_session(conn, principal.session_id)
        revoked = True
    clear_session_cookie(response, request)
    return {"ok": True, "revoked": revoked}


@router.get("/me")
def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    return {"user": principal.as_user(), "via": principal.via}


# --- API tokens ------------------------------------------------------------------


@router.post("/tokens")
def create_token(
    body: TokenIn, principal: Principal = Depends(current_principal), conn: psycopg.Connection = Depends(get_db)
) -> dict[str, Any]:
    """Returns the plaintext token exactly once."""
    token_id, plaintext = tokens.create_token(conn, principal.tenant_id, principal.user_id, body.name)
    return {"id": token_id, "name": body.name, "token": plaintext}


@router.get("/tokens")
def list_tokens(
    principal: Principal = Depends(current_principal), conn: psycopg.Connection = Depends(get_db)
) -> list[dict[str, Any]]:
    return tokens.list_tokens(conn, principal.user_id)


@router.delete("/tokens/{token_id}")
def revoke_token(
    token_id: str, principal: Principal = Depends(current_principal), conn: psycopg.Connection = Depends(get_db)
) -> dict[str, Any]:
    if not tokens.revoke_token(conn, token_id, principal.user_id):
        raise HTTPException(status_code=404, detail="token not found")
    return {"ok": True, "id": token_id}


# --- Slack identity ----------------------------------------------------------------


@router.post("/slack/link")
def slack_link(
    body: SlackLinkIn, principal: Principal = Depends(current_principal), conn: psycopg.Connection = Depends(get_db)
) -> dict[str, Any]:
    """Map the caller's Slack user id to their StartupOS user so Slack approve/decline records a real identity."""
    conn.execute(
        "UPDATE users SET slack_user_id = NULL WHERE slack_user_id = %s AND id <> %s",
        (body.slack_user_id, principal.user_id),
    )
    conn.execute("UPDATE users SET slack_user_id = %s WHERE id = %s", (body.slack_user_id, principal.user_id))
    return {"ok": True, "user_id": principal.user_id, "slack_user_id": body.slack_user_id}
