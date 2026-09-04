"""Pydantic models mirroring CONTRACTS.md. The API serializes these; the web app consumes them."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["reply", "click", "open", "bounce"]
Severity = Literal["high", "medium", "low", "info"]
ApprovalStatus = Literal["pending", "approved", "declined", "executed", "failed"]


class Tile(BaseModel):
    label: str
    val: str
    sub: str = ""
    cls: Literal["", "warn", "bad"] = ""


class Exec(BaseModel):
    server: Literal["Linear", "Slack"]
    tool: str
    input: dict[str, Any]


class Approval(BaseModel):
    id: str
    tenant_id: str
    module: str
    type: str
    target: str
    preview: str
    exec: Exec | None = None
    status: ApprovalStatus = "pending"
    created_by_run: str | None = None
    signal_id: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    decline_reason: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime | None = None


class Signal(BaseModel):
    id: str
    tenant_id: str
    module: str
    rule_id: str
    severity: Severity
    kind: Kind
    title: str
    meta: str = ""
    entity: str | None = None
    entity_id: str | None = None
    suggested_skill: str | None = None
    href: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    resolved_at: datetime | None = None


class SignalView(BaseModel):
    id: str
    kind: Kind
    title: str
    meta: str = ""
    action: str = ""
    href: str | None = None
    approval_id: str | None = None


class Table(BaseModel):
    title: str
    accent: str = ""
    cols: list[str]
    rows: list[list[str]]


class ModuleSnapshot(BaseModel):
    name: str
    title: str
    crumb: str
    source: str
    snapshot_at: datetime
    live: bool = False
    memory: str = ""
    tiles: list[Tile] = Field(min_length=4, max_length=4)
    signals: list[SignalView] = []
    table: Table | None = None
    table2: Table | None = None
    approvals: list[Approval] = []
    next: list[str] = []


class LLMResult(BaseModel):
    text: str
    model: str
    tier: int
    tokens_in: int = 0
    tokens_cached: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    run_id: str


class MemoryCard(BaseModel):
    slice: Literal["identity", "icp", "voice", "pricing", "team"]
    draft: str
    sources: list[str] = []


class Decision(BaseModel):
    decision: Literal["approve", "decline"]
    reason: str | None = None
    edited_preview: str | None = None
    decided_by: str = "owner"
