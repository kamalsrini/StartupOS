"""Slack ingest: conversations.history for the connection's `channels` → `messages` (source='slack')."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import psycopg

from ingest import fixtures
from ingest.base import Upserter, UpsertResult, parse_ts

log = logging.getLogger(__name__)

KEY_REF = "env:SLACK_BOT_TOKEN"

messages_upserter = Upserter(
    "messages",
    "slack",
    ["source", "id"],
    ["source", "id", "channel", "author", "text", "occurred_at", "thread_id", "raw"],
    json_cols=["raw"],
)


def normalize_message(raw: dict[str, Any], channel: str) -> dict[str, Any] | None:
    ts = raw.get("ts")
    text = raw.get("text") or ""
    if not ts:
        return None
    if raw.get("subtype") in {"channel_join", "channel_leave"} or not text.strip():
        return None
    thread = raw.get("thread_ts")
    return {
        "source": "slack",
        "id": str(ts),
        "channel": channel,
        "author": raw.get("user") or raw.get("username") or raw.get("bot_id"),
        "text": text,
        "occurred_at": parse_ts(float(ts)),
        "thread_id": str(thread) if thread and str(thread) != str(ts) else None,
        "raw": raw,
    }


def from_fixture(base: Path | None = None) -> list[dict[str, Any]]:
    data = fixtures.load("slack_messages.json", base)
    out: list[dict[str, Any]] = []
    for channel_id, ch in data["channels"].items():
        for m in ch.get("messages", []):
            row = normalize_message(m, channel_id)
            if row:
                out.append(row)
    return out


def fetch_live(token: str, channels: tuple[str, ...] | list[str] | None = None, limit: int = 500) -> list[dict]:
    if not token:
        raise RuntimeError("slack: no bot token")
    if not channels:
        raise RuntimeError("slack: no channels configured (connections.config.channels)")
    from slack_sdk import WebClient

    client = WebClient(token=token)
    out: list[dict[str, Any]] = []
    for channel in channels:
        cursor = None
        fetched = 0
        while True:
            resp = client.conversations_history(channel=channel, cursor=cursor, limit=200)
            for m in resp.get("messages", []):
                row = normalize_message(m, channel)
                if row:
                    out.append(row)
            fetched += len(resp.get("messages", []))
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor or fetched >= limit:
                break
    return out


def sync(
    conn: psycopg.Connection,
    tenant_id: str,
    *,
    api_key: str | None = None,
    use_fixtures: bool = False,
    fixtures_dir: Path | None = None,
    channels: tuple[str, ...] | list[str] | None = None,
    **_config: Any,
) -> dict[str, UpsertResult]:
    """Upsert messages for `tenant_id`. Token and channels come from the runner (connection row), never from env."""
    rows = from_fixture(fixtures_dir) if use_fixtures else fetch_live(api_key or "", channels)
    return {"messages": messages_upserter.upsert(conn, tenant_id, rows)}
