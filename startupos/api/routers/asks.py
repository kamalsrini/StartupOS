"""Asks queue — the web's way to invoke the daemon (Ask the OS / Chief of Staff) without the API ever calling a model.

POST /asks {question, mode} → {id, status: pending}; the daemon answers within ~30s; GET /asks/{id} polls.
"""

from __future__ import annotations

import secrets
from typing import Any, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.deps import current_principal, get_db, iso
from auth.identity import Principal

router = APIRouter(prefix="/asks", tags=["asks"])


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal["answer", "cos"] = "answer"


def _row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": r["id"],
        "mode": r["mode"],
        "question": r["question"],
        "status": r["status"],
        "answer": r["answer"],
        "run_id": r["run_id"],
        "created_at": iso(r["created_at"]),
        "answered_at": iso(r["answered_at"]) if r["answered_at"] else None,
    }


@router.post("", status_code=202)
def create_ask(
    body: AskIn, conn: psycopg.Connection = Depends(get_db), principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    ask_id = "ask_" + secrets.token_hex(8)
    row = conn.execute(
        """INSERT INTO asks (id, tenant_id, user_id, mode, question) VALUES (%s, %s, %s, %s, %s)
           RETURNING *""",
        (ask_id, principal.tenant_id, principal.user_id, body.mode, body.question),
    ).fetchone()
    return _row(row)


@router.get("/{ask_id}")
def get_ask(
    ask_id: str, conn: psycopg.Connection = Depends(get_db), principal: Principal = Depends(current_principal)
) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM asks WHERE id=%s AND tenant_id=%s", (ask_id, principal.tenant_id)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="ask not found")
    return _row(row)


@router.get("")
def list_asks(
    limit: int = 20, conn: psycopg.Connection = Depends(get_db), principal: Principal = Depends(current_principal)
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM asks WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
        (principal.tenant_id, min(limit, 100)),
    ).fetchall()
    return [_row(r) for r in rows]
