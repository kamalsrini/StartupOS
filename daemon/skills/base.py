"""Skill contract and shared helpers.

ctx = {conn, tenant_id, now, pack, settings} (+ "question" for ask.answer).
A skill returns list[Approval] (proposals it inserted) or str (text it produced).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from common.models import Approval
from common.settings import settings

Ctx = dict[str, Any]
RunFn = Callable[[Ctx], list[Approval] | str]


@dataclass
class Skill:
    name: str  # e.g. "build.assign_owner"
    module: str  # sales | finance | build | customers | marketing | web | social | security | cockpit | ask
    tier: int  # 0 | 1 | 2
    trigger: str  # schedule | signal | ask
    run: RunFn
    high_priority: bool = False
    description: str = field(default="")


REGISTRY: dict[str, Skill] = {}


def register(skill: Skill) -> Skill:
    REGISTRY[skill.name] = skill
    return skill


def latest_pack(conn: psycopg.Connection, tenant_id: str) -> str:
    """Latest compiled context pack (written by brain/pack.py). Empty string when none exists yet."""
    row = conn.execute(
        "SELECT content FROM context_packs WHERE tenant_id = %s ORDER BY compiled_at DESC, id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    return row["content"] if row else ""


def build_ctx(conn: psycopg.Connection, tenant_id: str | None = None, now: datetime | None = None, **extra: Any) -> Ctx:
    tenant_id = tenant_id or settings.tenant_id
    ctx: Ctx = {
        "conn": conn,
        "tenant_id": tenant_id,
        "now": now or datetime.now(UTC),
        "pack": latest_pack(conn, tenant_id),
        "settings": settings,
    }
    ctx.update(extra)
    return ctx


def system_prompt(ctx: Ctx, instructions: str) -> str:
    """Context pack + skill instructions, sent as one cacheable system block by daemon.llm."""
    pack = ctx.get("pack") or "(no context pack compiled yet)"
    return f"{pack}\n\n---\n\n# Skill instructions\n\n{instructions.strip()}\n"


def plus_days(ctx: Ctx, days: int) -> str:
    return (ctx["now"] + timedelta(days=days)).date().isoformat()


def open_signals(conn: psycopg.Connection, tenant_id: str, rule_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT * FROM signals WHERE tenant_id = %s AND rule_id = %s AND resolved_at IS NULL
               ORDER BY severity, last_seen_at DESC""",
            (tenant_id, rule_id),
        ).fetchall()
    ]


def signal_exists(conn: psycopg.Connection, signal_id: str | None) -> str | None:
    """approvals.signal_id is a FK; only link a signal that is actually present."""
    if not signal_id:
        return None
    row = conn.execute("SELECT id FROM signals WHERE id = %s", (signal_id,)).fetchone()
    return row["id"] if row else None


def approval_exists(conn: psycopg.Connection, approval_id: str) -> bool:
    return conn.execute("SELECT 1 FROM approvals WHERE id = %s", (approval_id,)).fetchone() is not None
