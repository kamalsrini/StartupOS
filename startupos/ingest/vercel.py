"""Vercel ingest (optional): deployments → `deployments`. Live via REST v6, or from fixtures."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import psycopg

from common.settings import settings
from ingest import fixtures
from ingest.base import Upserter, UpsertResult, parse_ts

log = logging.getLogger(__name__)

KEY_REF = "env:VERCEL_TOKEN"
DEPLOYMENTS_URL = "https://api.vercel.com/v6/deployments"

deployments_upserter = Upserter(
    "deployments",
    "vercel",
    ["id"],
    ["id", "project", "state", "target", "commit_message", "created_at", "url"],
)


def normalize_deployment(raw: dict[str, Any]) -> dict[str, Any]:
    meta = raw.get("meta") or {}
    return {
        "id": raw.get("uid") or raw["id"],
        "project": raw.get("name") or raw.get("project") or "unknown",
        "state": raw.get("state") or raw.get("readyState"),
        "target": raw.get("target"),
        "commit_message": meta.get("githubCommitMessage")
        or meta.get("gitlabCommitMessage")
        or raw.get("commit_message"),
        "created_at": parse_ts(raw.get("created") or raw.get("createdAt")),
        "url": raw.get("inspectorUrl") or (f"https://{raw['url']}" if raw.get("url") else None),
    }


def from_fixture(base: Path | None = None) -> list[dict[str, Any]]:
    data = fixtures.load("vercel_deployments.json", base)
    deployments = data.get("deployments", data)
    if isinstance(deployments, dict):
        deployments = deployments.get("deployments", [])
    return [normalize_deployment(d) for d in deployments]


def fetch_live(token: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    tok = token or settings.secret(KEY_REF)
    if not tok:
        raise RuntimeError("VERCEL_TOKEN not configured")
    params: dict[str, Any] = {"limit": limit}
    if settings.vercel_team_id:
        params["teamId"] = settings.vercel_team_id
    with httpx.Client() as client:
        resp = client.get(DEPLOYMENTS_URL, params=params, headers={"Authorization": f"Bearer {tok}"}, timeout=30.0)
        resp.raise_for_status()
        return [normalize_deployment(d) for d in resp.json().get("deployments", [])]


def has_credentials() -> bool:
    return bool(settings.secret(KEY_REF))


def sync(conn: psycopg.Connection, tenant_id: str, *, use_fixtures: bool = False) -> dict[str, UpsertResult]:
    rows = from_fixture() if use_fixtures else fetch_live()
    return {"deployments": deployments_upserter.upsert(conn, tenant_id, rows)}
