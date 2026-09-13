"""Slack delivery — the founder never has to open the web app (Architecture Brief §4 Cockpit, §5 Everyday).

    deliver(conn, tenant_id, kind, payload, *, channel=None) -> dict

One function, four kinds (`pulse`, `digest`, `signal`, `approval`), and four rules:

1. **Channel.** Explicit `channel=` → `tenants.slack_channel` → the install's `default_channel` → skip. Whether
   to post at all is `tenants.pulse_channel`: `web` never posts, `slack`/`both` do (`both` still returns the text
   to the web caller, because delivery never owns the return value of a skill).
2. **Token.** `common.secrets.credential_for_source(conn, tenant_id, "slack")` — the tenant's own bot token, the
   one the Slack install stored. Never the environment; a tenant with no Slack credential skips cleanly — and
   writes NO ledger row, because "Slack is not connected yet" must not consume the ref (see `undelivered_signals`).
3. **Dedupe is the UNIQUE constraint, not a query.** Every delivery claims `deliveries (tenant_id, kind, ref)`
   before posting. A conflicting claim against a row that is already `sent` (or in flight) means "already
   delivered" and returns `skipped`. A previous `skipped`/`failed` row is re-claimed, so connecting Slack after a
   skipped pulse does not blacklist that ref forever.
4. **Failures never break the caller.** Every exception below the API boundary is caught, recorded on the
   deliveries row as `failed` with its message, logged once, and returned as a dict. `deliver` never raises for
   a Slack, credential or channel problem; only a programming error (an unknown `kind`) raises.

Posting goes through `daemon/executors/slack.py::post_message`, which stays the only Slack write path.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

import psycopg

from common import secrets
from common.settings import settings
from daemon import slack_blocks
from daemon.executors import slack as slack_executor

log = logging.getLogger("daemon.delivery")

KINDS = ("pulse", "digest", "signal", "approval")
POSTING_MODES = ("slack", "both")  # tenants.pulse_channel values that post; 'web' never does
SIGNALS_PER_TICK = 5


class UnknownKind(ValueError):
    """A kind outside KINDS — a programming error, not a delivery failure."""


# --- channel + token resolution -------------------------------------------------------------------


def tenant_delivery_settings(conn: psycopg.Connection, tenant_id: str) -> dict[str, Any]:
    """`{mode, channel}` from `tenants`. Missing row → the safe default: web, no channel."""
    row = conn.execute("SELECT pulse_channel, slack_channel FROM tenants WHERE id = %s", (tenant_id,)).fetchone() or {
        "pulse_channel": "web",
        "slack_channel": None,
    }
    mode = (row.get("pulse_channel") or "web").strip()
    channel = (row.get("slack_channel") or "").strip() or None
    if mode not in ("web", *POSTING_MODES):
        # Direct form: pulse_channel naming a channel rather than a surface ("#ops", "C0789").
        channel, mode = mode, "slack"
    return {"mode": mode, "channel": channel}


def install_channel(conn: psycopg.Connection, tenant_id: str) -> str | None:
    """The Slack install's `default_channel` (Track I's table). Absent table or row → None, never an error."""
    try:
        if conn.execute("SELECT to_regclass('public.slack_installations') AS t").fetchone()["t"] is None:
            return None
        row = conn.execute(
            "SELECT default_channel FROM slack_installations WHERE tenant_id = %s AND revoked_at IS NULL",
            (tenant_id,),
        ).fetchone()
    except Exception:  # the install table is another track's; delivery degrades to "no channel", never crashes
        log.debug("slack_installations unreadable for %s", tenant_id)
        return None
    return ((row or {}).get("default_channel") or "").strip() or None


def resolve_channel(conn: psycopg.Connection, tenant_id: str, explicit: str | None = None) -> dict[str, Any]:
    """`{channel, mode, reason}`. `channel` None means "do not post", with `reason` saying why."""
    cfg = tenant_delivery_settings(conn, tenant_id)
    if cfg["mode"] == "web":
        return {"channel": None, "mode": "web", "reason": "pulse_channel=web (Slack delivery off)"}
    channel = (explicit or "").strip() or cfg["channel"] or install_channel(conn, tenant_id)
    if not channel:
        return {"channel": None, "mode": cfg["mode"], "reason": "no Slack channel configured"}
    return {"channel": channel, "mode": cfg["mode"], "reason": ""}


def slack_token(conn: psycopg.Connection, tenant_id: str) -> str | None:
    """This tenant's own bot token, or None. A secrets problem is a skip, never an exception out of delivery."""
    try:
        return secrets.credential_for_source(conn, tenant_id, "slack")
    except Exception as exc:
        log.warning("no usable Slack credential for %s: %s", tenant_id, type(exc).__name__)
        return None


# --- refs and rendering ---------------------------------------------------------------------------


def _day(value: Any) -> str:
    if isinstance(value, datetime | date):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    return str(value) if value else datetime.now(UTC).date().isoformat()


def default_ref(kind: str, payload: dict[str, Any]) -> str:
    """`signal:<id>` · `pulse:<YYYY-MM-DD>` · `digest:<YYYY-MM-DD>` · `approval:<id>`."""
    if kind in ("pulse", "digest"):
        return f"{kind}:{_day(payload.get('date') or payload.get('now'))}"
    if kind == "signal":
        return f"signal:{(payload.get('signal') or payload).get('id')}"
    if kind == "approval":
        return f"approval:{(payload.get('approval') or payload).get('id')}"
    raise UnknownKind(kind)


def render(kind: str, payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """`(fallback_text, blocks)` for one kind. Pure — everything it needs is in `payload`."""
    web_url = payload.get("web_url") or settings.web_url
    if kind == "pulse":
        return slack_blocks.pulse_blocks(
            payload.get("text") or "",
            payload.get("tiles") or [],
            int(payload.get("approvals_count") or 0),
            web_url,
        )
    if kind == "digest":
        return slack_blocks.digest_blocks({**payload, "web_url": web_url})
    if kind == "signal":
        return slack_blocks.signal_blocks(payload.get("signal") or payload, overflow=int(payload.get("overflow") or 0))
    if kind == "approval":
        return slack_blocks.approval_blocks(payload.get("approval") or payload, decided=payload.get("decided"))
    raise UnknownKind(kind)


# --- the deliveries ledger ------------------------------------------------------------------------


def _claim(conn: psycopg.Connection, tenant_id: str, kind: str, ref: str, channel: str | None) -> int | None:
    """Claim `(tenant_id, kind, ref)` as `pending`. None = someone already delivered (or is delivering) it.

    The UNIQUE constraint is the lock: two daemons on the same ref, one row. A row that never reached `sent`
    (skipped for a missing channel, failed on a Slack error) is re-claimable so the next tick can try again.
    """
    row = conn.execute(
        """INSERT INTO deliveries (tenant_id, kind, ref, channel, status)
           VALUES (%s, %s, %s, %s, 'pending')
           ON CONFLICT (tenant_id, kind, ref) DO UPDATE
             SET status = 'pending', channel = EXCLUDED.channel, error = NULL, ts = NULL, created_at = now()
             WHERE deliveries.status IN ('skipped', 'failed')
                OR (deliveries.status = 'pending' AND deliveries.created_at < now() - interval '15 minutes')
           RETURNING id""",
        (tenant_id, kind, ref, channel),
    ).fetchone()
    return row["id"] if row else None


def _finish(
    conn: psycopg.Connection, delivery_id: int, status: str, *, ts: str | None = None, error: str | None = None
) -> None:
    conn.execute(
        "UPDATE deliveries SET status = %s, ts = %s, error = %s WHERE id = %s",
        (status, ts, (error or None) and error[:500], delivery_id),
    )


def _result(status: str, ref: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "ref": ref, **extra}


# --- the one entry point --------------------------------------------------------------------------


def deliver(
    conn: psycopg.Connection,
    tenant_id: str,
    kind: str,
    payload: dict[str, Any],
    *,
    channel: str | None = None,
) -> dict[str, Any]:
    """Post one message to the tenant's Slack and record it. Returns `{status, ref, channel?, ts?, error?, reason?}`.

    `status` is `sent` | `skipped` | `failed`; it never raises for a delivery problem. `payload` carries what the
    renderer for `kind` needs (see `render`) and may carry an explicit `ref`.
    """
    if kind not in KINDS:
        raise UnknownKind(f"{kind!r} is not one of {KINDS}")
    payload = dict(payload or {})
    ref = str(payload.get("ref") or default_ref(kind, payload))

    target = resolve_channel(conn, tenant_id, channel)
    if not target["channel"]:
        return _skip(conn, tenant_id, kind, ref, None, target["reason"])

    token = slack_token(conn, tenant_id)
    if not token:
        # No ledger row (PE follow-up): "Slack is not connected yet" is a transient state, not a decision. A
        # `skipped` row would consume the ref — `undelivered_signals` treats any non-`failed` row as handled —
        # so every open signal would be silently swallowed on the first tick after deploy and never posted once
        # Slack is connected. Writing nothing leaves the ref unclaimed, so the next tick delivers it.
        return _result("skipped", ref, channel=target["channel"], reason="no Slack credential for this tenant")

    delivery_id = _claim(conn, tenant_id, kind, ref, target["channel"])
    if delivery_id is None:
        return _result("skipped", ref, channel=target["channel"], reason="already delivered")

    try:
        text, blocks = render(kind, payload)
        problems = slack_blocks.validate(blocks)
        if problems:  # never send a message Slack would reject; the ledger says exactly what was wrong
            raise ValueError(f"invalid Block Kit: {'; '.join(problems)}")
        out = slack_executor.post_message({"channel": target["channel"], "text": text, "blocks": blocks}, token=token)
    except Exception as exc:
        message = f"{type(exc).__name__}: {str(exc)[:400]}"
        log.warning("delivery %s %s for %s failed: %s", kind, ref, tenant_id, message)
        _finish(conn, delivery_id, "failed", error=message)
        return _result("failed", ref, channel=target["channel"], error=message)

    ts = str(out.get("ts") or "")
    _finish(conn, delivery_id, "sent", ts=ts or None)
    return _result("sent", ref, channel=out.get("channel") or target["channel"], ts=ts, url=out.get("url"))


def _skip(
    conn: psycopg.Connection, tenant_id: str, kind: str, ref: str, channel: str | None, reason: str
) -> dict[str, Any]:
    """Record a delivery that never reached Slack. A skip is normal (a web-only tenant, no Slack yet)."""
    delivery_id = _claim(conn, tenant_id, kind, ref, channel)
    if delivery_id is None:
        return _result("skipped", ref, channel=channel, reason="already delivered")
    _finish(conn, delivery_id, "skipped", error=reason)
    return _result("skipped", ref, channel=channel, reason=reason)


def approval_dict(approval: Any) -> dict[str, Any]:
    """An approval as a plain dict, whether it arrives as a model, a row or a dict."""
    if approval is None:
        return {}
    if isinstance(approval, dict):
        return dict(approval)
    if hasattr(approval, "model_dump"):
        return dict(approval.model_dump())
    return dict(approval)


def deliver_approval(
    conn: psycopg.Connection,
    tenant_id: str,
    approval: Any,
    *,
    channel: str | None = None,
    decided: Any = None,
) -> dict[str, Any]:
    """Post one approval with its Approve/Decline buttons (Sprint 3b, Track B).

    `approval_blocks` renders both states, so `decided=` posts the resolved form instead (rarely wanted — Track B
    updates the original message in place rather than posting a second one). Records `approval:<id>` in the
    deliveries ledger with the message `ts` that `chat.update` needs, and returns `deliver`'s dict: never raises
    for a delivery problem, so proposing an approval cannot fail because Slack is down.
    """
    data = approval_dict(approval)
    payload: dict[str, Any] = {"approval": data, "ref": f"approval:{data.get('id')}"}
    if decided is not None:
        payload["decided"] = approval_dict(decided) if not isinstance(decided, str) else decided
    return deliver(conn, tenant_id, "approval", payload, channel=channel)


# --- payload builders the scheduler and the cockpit skills use --------------------------------------


def pulse_payload(conn: psycopg.Connection, tenant_id: str, text: str, now: datetime | None = None) -> dict[str, Any]:
    """The pulse payload with Tier-0 tiles — three counts already on the connection, no model, no extra call."""
    now = now or datetime.now(UTC)
    counts = conn.execute(
        """SELECT (SELECT count(*) FROM approvals WHERE tenant_id = %s AND status = 'pending') AS approvals,
                  (SELECT count(*) FROM signals WHERE tenant_id = %s AND resolved_at IS NULL) AS open_signals,
                  (SELECT count(*) FROM signals WHERE tenant_id = %s AND resolved_at IS NULL AND severity = 'high')
                    AS high_signals""",
        (tenant_id, tenant_id, tenant_id),
    ).fetchone()
    return {
        "ref": f"pulse:{_day(now)}",
        "date": now,
        "text": text,
        "approvals_count": int(counts["approvals"]),
        "tiles": [
            {"label": "high signals", "val": str(counts["high_signals"])},
            {"label": "open signals", "val": str(counts["open_signals"])},
            {"label": "approvals waiting", "val": str(counts["approvals"])},
        ],
    }


def digest_payload(conn: psycopg.Connection, tenant_id: str, text: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    counts = conn.execute(
        """SELECT (SELECT count(*) FROM approvals WHERE tenant_id = %s AND status = 'pending') AS approvals,
                  (SELECT count(*) FROM signals WHERE tenant_id = %s AND resolved_at IS NULL) AS open_signals""",
        (tenant_id, tenant_id),
    ).fetchone()
    return {
        "ref": f"digest:{_day(now)}",
        "date": now,
        "text": text,
        "approvals_pending": int(counts["approvals"]),
        "open_signals": int(counts["open_signals"]),
    }


def deliver_text(
    conn: psycopg.Connection, tenant_id: str, kind: str, text: str, now: datetime | None = None
) -> dict[str, Any]:
    """Deliver a pulse/digest the skill just produced. Swallows everything: a Slack problem must never turn a
    successful (and already paid for) Tier-2 run into a failed one — the skill still returns its text to the web."""
    try:
        builder = pulse_payload if kind == "pulse" else digest_payload
        return deliver(conn, tenant_id, kind, builder(conn, tenant_id, text, now))
    except Exception as exc:  # includes a DB error in the payload builder, which deliver() cannot catch
        log.exception("%s delivery for %s raised: %s", kind, tenant_id, type(exc).__name__)
        return {"status": "failed", "ref": f"{kind}:{_day(now)}", "error": type(exc).__name__}


def undelivered_signals(conn: psycopg.Connection, tenant_id: str, limit: int = SIGNALS_PER_TICK) -> dict[str, Any]:
    """`{rows, remaining}` — open `high` signals (and every `cos.risk`) with no delivery row yet, oldest first.

    `remaining` is how many are left beyond `limit`, which is the number the overflow line quotes. A delivery row
    in any state except `failed` means "already handled": a `skipped` signal (a web-only tenant — a deliberate
    "never post") is not resurrected days later, while a `failed` post is retried on the next tick. A tenant with
    no Slack credential yet gets no row at all (`deliver`), so its backlog is still here when Slack is connected.
    """
    rows = conn.execute(
        """WITH pending AS (
             SELECT s.id, s.module, s.rule_id, s.severity, s.kind, s.title, s.meta, s.href, s.suggested_skill,
                    s.first_seen_at
               FROM signals s
              WHERE s.tenant_id = %s AND s.resolved_at IS NULL
                AND (s.severity = 'high' OR s.rule_id LIKE 'cos.risk%%')
                AND NOT EXISTS (SELECT 1 FROM deliveries d
                                 WHERE d.tenant_id = s.tenant_id AND d.kind = 'signal'
                                   AND d.ref = 'signal:' || s.id AND d.status <> 'failed'))
           SELECT *, count(*) OVER () AS total FROM pending ORDER BY first_seen_at, id LIMIT %s""",
        (tenant_id, max(1, limit)),
    ).fetchall()
    rows = [dict(r) for r in rows]
    total = int(rows[0]["total"]) if rows else 0
    for r in rows:
        r.pop("total", None)
        r.pop("first_seen_at", None)
    return {"rows": rows, "remaining": max(0, total - len(rows))}


def deliver_signals(conn: psycopg.Connection, tenant_id: str, limit: int = SIGNALS_PER_TICK) -> list[dict[str, Any]]:
    """One message per new high/Chief-of-Staff signal, capped at `limit` per tick with an overflow line on the last.

    Called from the 15-minute tick. Never raises: a failing post is recorded and the loop continues.
    """
    batch = undelivered_signals(conn, tenant_id, limit)
    out: list[dict[str, Any]] = []
    rows = batch["rows"]
    for i, signal in enumerate(rows):
        overflow = batch["remaining"] if i == len(rows) - 1 else 0
        try:
            out.append(deliver(conn, tenant_id, "signal", {"signal": signal, "overflow": overflow}))
        except Exception as exc:  # deliver() catches its own; this is the belt to its braces
            log.exception("signal delivery %s for %s raised: %s", signal.get("id"), tenant_id, type(exc).__name__)
            out.append(_result("failed", f"signal:{signal.get('id')}", error=type(exc).__name__))
    return out
