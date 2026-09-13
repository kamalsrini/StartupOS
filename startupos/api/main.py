"""StartupOS API. `uvicorn api.main:app --reload --port 8000`.

Rules: reads Postgres tables directly; never calls a model; never holds or returns a secret (connections carry
secret_ref only); deciding an approval only flips pending → approved|declined — execution is the daemon's job.

Auth: every route except /health, /auth/google, /auth/google/callback, /auth/bootstrap and POST /onboarding/tenant
requires a principal (Bearer token or sos_session cookie); the tenant is always derived from the user.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.deps import api_dsn, optional_principal
from api.routers import approvals, asks, auth, cockpit, finance, modules, onboarding
from auth import config
from auth.identity import Principal
from common import secrets
from common.db import get_conn


def check_startup_config() -> None:
    """Refuse to start without a session secret unless STARTUPOS_DEV=1 (which uses a logged, insecure fallback)."""
    if not config._get("session_secret", "STARTUPOS_SESSION_SECRET") and not config.dev_mode():
        raise RuntimeError(
            "STARTUPOS_SESSION_SECRET is not set. Generate one (`openssl rand -hex 32`) and put it in .env, "
            "or export STARTUPOS_DEV=1 for local development only."
        )
    config.session_secret()  # logs the dev warning once
    secrets.check_master_key(logging.getLogger("api"))  # malformed → RuntimeError; unset → one warning (503 on writes)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    check_startup_config()
    yield


app = FastAPI(title="StartupOS API", version="0.1.0", docs_url="/docs", lifespan=lifespan)

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

app.include_router(auth.router)
app.include_router(modules.router)
app.include_router(finance.router)
app.include_router(approvals.router)
app.include_router(onboarding.router)
app.include_router(cockpit.router)
app.include_router(asks.router)


@app.get("/health")
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


@app.get("/")
def root() -> dict:
    return {"name": "StartupOS API", "docs": "/docs", "health": "/health", "dsn_configured": bool(api_dsn())}
