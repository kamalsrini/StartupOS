"""StartupOS API. `uvicorn api.main:app --reload --port 8000`.

Rules: reads Postgres tables directly; never calls a model; never holds or returns a secret (connections carry
secret_ref only); deciding an approval only flips pending → approved|declined — execution is the daemon's job.

Auth: every route except the ones in PUBLIC_PATHS below requires a principal (Bearer token or sos_session cookie);
the tenant is always derived from the user. The public list is enumerated once, here, and a test walks
`app.routes` to prove nothing else slipped out of the fence.

Path prefix (Sprint 3d): STARTUPOS_PATH_PREFIX mounts every route under a prefix (`/api`) so one hostname can
serve the web app at `/` and this API at `/api/...` behind a proxy that does NOT strip the prefix. Empty (the
default, and what local dev and CI use) is byte-for-byte the previous behaviour. The prefix is read once at
import: tests that exercise the prefixed mode reload this module with the env var set.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.deps import api_dsn, optional_principal
from api.routers import approvals, asks, auth, cockpit, finance, modules, onboarding, slack
from auth import config, slack_install
from auth.identity import Principal
from common import secrets
from common.db import get_conn

# Routes that answer without a StartupOS principal, and what authenticates them instead. Everything else needs a
# Bearer token or a session cookie; tests/functional/test_slack_install.py walks app.routes and asserts exactly this.
#   /health, /                                  public by design, no tenant data
#   /docs, /redoc, /openapi.json                dev only — see DOCS_ENABLED below; absent on a public deploy
#   /auth/google*, /auth/bootstrap              the sign-in flow itself (state cookie / bootstrap token)
#   POST /onboarding/tenant                     sign-up (bootstrap token or a verified Google id_token)
#   /slack/oauth/callback                       the signed `state` token carries the tenant (10-minute max age)
#   /slack/events, /slack/interactivity         Slack's HMAC signature (auth/slack_sig.py) + team_id → installation
# Sprint 3d PE review: the interactive API documentation is not served on a public deployment. It leaks no
# tenant data, but it hands anyone with the URL the full route, parameter and schema map of the product, and
# `config.docs_enabled()` keys off the variables a public deploy cannot work without — so nobody can forget to
# turn it off. When it is off the three routes do not exist at all (FastAPI never mounts them), which is why
# they leave PUBLIC_PATHS too: the "everything not enumerated is 401" walker must stay exactly accurate.
DOCS_ENABLED = config.docs_enabled()

_BASE_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/health",
        "/auth/providers",
        # Signing out is public on purpose (Sprint 3d, Track G): it revokes only the caller's own session and
        # clears only the caller's own cookie, and it has to work when that session has ALREADY expired —
        # otherwise the dead cookie stays in the browser and the person cannot get back to a sign-in page.
        "/auth/logout",
        "/auth/google",
        "/auth/google/callback",
        "/auth/bootstrap",
        "/onboarding/tenant",
        "/slack/oauth/callback",
        "/slack/events",
        "/slack/interactivity",
    }
)

if DOCS_ENABLED:
    _BASE_PUBLIC_PATHS |= {"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}

PATH_PREFIX = config.path_prefix()
# The paths as actually mounted. With no prefix this is _BASE_PUBLIC_PATHS unchanged.
PUBLIC_PATHS: frozenset[str] = frozenset(config.prefixed(p) for p in _BASE_PUBLIC_PATHS)


def check_startup_config() -> None:
    """Refuse to start without a session secret unless STARTUPOS_DEV=1 (which uses a logged, insecure fallback)."""
    if not config._get("session_secret", "STARTUPOS_SESSION_SECRET") and not config.dev_mode():
        raise RuntimeError(
            "STARTUPOS_SESSION_SECRET is not set. Generate one (`openssl rand -hex 32`) and put it in .env, "
            "or export STARTUPOS_DEV=1 for local development only."
        )
    config.session_secret()  # logs the dev warning once
    secrets.check_master_key(logging.getLogger("api"))  # malformed → RuntimeError; unset → one warning (503 on writes)
    slack_install.warn_if_unconfigured(logging.getLogger("api"))  # no Slack app → /slack/* answer 503, nothing else
    mismatch = config.public_url_prefix_mismatch()
    if mismatch:
        logging.getLogger("api").warning("%s", mismatch)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    check_startup_config()
    yield


app = FastAPI(
    title="StartupOS API",
    version="0.1.0",
    docs_url=config.prefixed("/docs") if DOCS_ENABLED else None,
    redoc_url=config.prefixed("/redoc") if DOCS_ENABLED else None,
    openapi_url=config.prefixed("/openapi.json") if DOCS_ENABLED else None,
    swagger_ui_oauth2_redirect_url=config.prefixed("/docs/oauth2-redirect") if DOCS_ENABLED else None,
    lifespan=lifespan,
)

_origins = [config.web_url()] + [
    o for o in (os.environ.get("STARTUPOS_CORS_ORIGINS") or "").split(",") if o and o != config.web_url()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix=PATH_PREFIX)
app.include_router(modules.router, prefix=PATH_PREFIX)
app.include_router(finance.router, prefix=PATH_PREFIX)
app.include_router(approvals.router, prefix=PATH_PREFIX)
app.include_router(onboarding.router, prefix=PATH_PREFIX)
app.include_router(cockpit.router, prefix=PATH_PREFIX)
app.include_router(asks.router, prefix=PATH_PREFIX)
app.include_router(slack.router, prefix=PATH_PREFIX)


@app.get(config.prefixed("/health"))
def health(principal: Principal | None = Depends(optional_principal)) -> dict:
    with get_conn(api_dsn(), tenant_id="") as conn:
        conn.execute("SELECT 1")
    # The DSN is never echoed; only whether a DB answered and, when signed in, which tenant the caller belongs to.
    return {
        "ok": True,
        "db": "up",
        "tenant": principal.tenant_id if principal else None,
        "dsn_source": "env" if os.environ.get("STARTUPOS_API_DSN") else "settings",
    }


@app.get(config.prefixed("/"))
def root() -> dict:
    # The links are given as mounted, so they are clickable behind the proxy as well as locally.
    return {
        "name": "StartupOS API",
        "docs": config.prefixed("/docs") if DOCS_ENABLED else None,
        "health": config.prefixed("/health"),
        "dsn_configured": bool(api_dsn()),
    }
