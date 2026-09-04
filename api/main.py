"""StartupOS API. `uvicorn api.main:app --reload --port 8000`.

Rules: reads Postgres tables directly; never calls a model; never holds or returns a secret (connections carry
secret_ref only); deciding an approval only flips pending → approved|declined — execution is the daemon's job.
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.deps import api_dsn, get_db, get_tenant
from api.routers import approvals, cockpit, finance, modules, onboarding

app = FastAPI(title="StartupOS API", version="0.1.0", docs_url="/docs")

_origins = [
    o
    for o in (os.environ.get("STARTUPOS_CORS_ORIGINS") or "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if o
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(modules.router)
app.include_router(finance.router)
app.include_router(approvals.router)
app.include_router(onboarding.router)
app.include_router(cockpit.router)


@app.get("/health")
def health(conn=Depends(get_db), tenant_id: str = Depends(get_tenant)) -> dict:
    conn.execute("SELECT 1")
    # The DSN is never echoed; only whether a DB answered and which tenant the request scopes to.
    return {
        "ok": True,
        "db": "up",
        "tenant": tenant_id,
        "dsn_source": "env" if os.environ.get("STARTUPOS_API_DSN") else "settings",
    }


@app.get("/")
def root() -> dict:
    return {"name": "StartupOS API", "docs": "/docs", "health": "/health", "dsn_configured": bool(api_dsn())}
