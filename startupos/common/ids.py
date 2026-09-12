"""Deterministic and random ids."""

from __future__ import annotations

import hashlib
import secrets


def run_id() -> str:
    return "run_" + secrets.token_hex(8)


def approval_id(kind: str, entity_id: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in f"{kind}-{entity_id}".lower())
    return slug[:80]


def content_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
