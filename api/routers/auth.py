"""Auth endpoints: Google OIDC, bootstrap, logout, me, API tokens, Slack link.

Public: GET /auth/google, GET /auth/google/callback, POST /auth/bootstrap. Everything else needs a principal.
Never logs or returns tokens, cookies, id_tokens, secrets or token hashes.
"""

from __future__ import annotations

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from api.deps import api_dsn, clear_session_cookie, cookie_secure_for, current_principal, get_db, set_session_cookie
from auth import bootstrap, config, google, sessions, tokens
from auth.identity import Principal
from common.db import get_conn
from common.settings import settings

router = APIRouter(prefix="/auth", tags=["auth"])

NO_ACCOUNT = "No account for this email — ask your company owner to invite you"


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
        path="/auth/google",
    )
    return resp


@router.get("/google/callback")
def google_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> Response:
    if error:
        raise HTTPException(status_code=403, detail="Google sign-in was cancelled")
    if not google.configured():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured (GOOGLE_CLIENT_ID)")
    saved = google.read_oauth_cookie(request.cookies.get(google.OAUTH_COOKIE))
    if not saved or not state or not code or saved[0] != state:
        raise HTTPException(status_code=403, detail="sign-in state mismatch — start again at /auth/google")
    _, nonce = saved
    try:
        token_resp = google.exchange_code(code)
        claims = google.verify_id_token(token_resp["id_token"], nonce)
    except google.GoogleAuthError:
        raise HTTPException(status_code=403, detail="Google sign-in failed") from None

    sub, email = str(claims["sub"]), str(claims["email"]).lower()
    with get_conn(api_dsn(), tenant_id="") as anon:
        hit = anon.execute("SELECT * FROM auth_lookup_google(%s, %s)", (sub, email)).fetchone()
    if not hit:
        return JSONResponse(status_code=403, content={"detail": NO_ACCOUNT})
    if hit["status"] != "active":
        return JSONResponse(status_code=403, content={"detail": NO_ACCOUNT})
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
    resp.delete_cookie(google.OAUTH_COOKIE, path="/auth/google")
    return resp


# --- bootstrap -----------------------------------------------------------------


class _Throttle:
    """In-process attempt limiter (PE review 2026-09-04): 5 attempts per source per minute is plenty for a
    one-time owner bootstrap and makes the high-entropy token effectively unguessable online."""

    def __init__(self, limit: int = 5, window_s: int = 60) -> None:
        self.limit, self.window, self._hits = limit, window_s, {}

    def allow(self, key: str) -> bool:
        import time

        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True


_bootstrap_throttle = _Throttle()


@router.post("/bootstrap")
def bootstrap_owner(body: BootstrapIn, request: Request, response: Response) -> dict[str, Any]:
    """Create/activate the first owner of settings.tenant_id and start a session. 404 when disabled, 403 on bad token."""
    if not bootstrap.enabled():
        raise HTTPException(status_code=404, detail="bootstrap is disabled")
    if not _bootstrap_throttle.allow(request.client.host if request.client else "?"):
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
    principal: Principal = Depends(current_principal),
    conn: psycopg.Connection = Depends(get_db),
) -> dict[str, Any]:
    if principal.session_id:
        sessions.revoke_session(conn, principal.session_id)
    clear_session_cookie(response, request)
    return {"ok": True}


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
