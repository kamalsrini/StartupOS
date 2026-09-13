"""Slack interactivity: the Approve/Decline buttons on an approval (CONTRACTS.md Sprint 3b, Track B).

    handle_action(payload, *, conn_factory) -> dict

`payload` is the already-parsed Slack interactivity payload; `POST /slack/interactivity` verifies Slack's
signature before calling and JSON-encodes whatever comes back as its 200. `conn_factory(tenant_id)` yields a
connection bound to that tenant (and, with `""`, an unbound one for the two SECURITY DEFINER lookups), so this
module never decides how to reach the database and tests hand it a fake.

Four rules, in this order, and none of them is optional:

1. **Identity before anything.** The workspace resolves to an installation (`team_id` → `slack_installations`,
   never a field the caller controls), and the pressing Slack user resolves to a user of THAT installation's
   tenant through `auth_lookup_slack`. A stranger — unmapped, or mapped to another tenant — changes nothing and
   gets an ephemeral "link your account".
2. **The gate is the only way in.** `approvals.decide` (its `SELECT … FOR UPDATE` is what makes a double click
   decide once) and then `executors.run_approved`, which re-reads the row and acts with the tenant's OWN
   credential. Nothing here calls an executor directly, and a declined approval never reaches one.
3. **The message is updated in place.** `chat.update` with `approval_blocks(decided=…)` replaces the buttons with
   the outcome, so they cannot be pressed twice. A second press of an already-decided approval decides nothing
   and simply re-renders; an approval with no delivered message (Slack connected after it was proposed) still
   decides — there is just nothing to update.
4. **Nothing here raises at the router.** Every failure below is a dict: an ephemeral reply for the human, a log
   line for us. Only an `action_id` we do not own raises `UnknownAction`, which the router turns into 404.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

from common.models import Approval, Decision
from daemon import approvals, delivery, executors, slack_blocks
from daemon.executors import slack as slack_executor

log = logging.getLogger("daemon.slack_actions")

APPROVE_ACTION = "approval_approve"
DECLINE_ACTION = "approval_decline"
KNOWN_ACTIONS: frozenset[str] = frozenset({APPROVE_ACTION, DECLINE_ACTION})

NOT_CONNECTED = "This Slack workspace is not connected to StartupOS. Ask an owner to run Connect Slack in Settings."
GONE = "That approval is no longer available."
DECLINE_REASON = "Declined from Slack"


class UnknownAction(Exception):
    """No handler for this action_id — the router answers 404 and nothing changes."""


# --- payload reading ------------------------------------------------------------------------------


def action_ids(payload: dict[str, Any]) -> list[str]:
    """Every `action_id` in a Slack interactivity payload (block_actions carries a list)."""
    return [
        str(a.get("action_id")) for a in (payload.get("actions") or []) if isinstance(a, dict) and a.get("action_id")
    ]


def approval_action(payload: dict[str, Any]) -> dict[str, Any]:
    """The one approval button in this payload: `{action_id, approval_id}`. Anything else is UnknownAction."""
    for action in payload.get("actions") or []:
        if isinstance(action, dict) and action.get("action_id") in KNOWN_ACTIONS:
            approval_id = str(action.get("value") or "").strip()
            if not approval_id:
                raise UnknownAction(f"{action['action_id']} carried no approval id")
            return {"action_id": str(action["action_id"]), "approval_id": approval_id}
    ids = action_ids(payload)
    raise UnknownAction(f"no handler for action_id {ids[0] if ids else '(none)'}")


def team_id(payload: dict[str, Any]) -> str:
    return str((payload.get("team") or {}).get("id") or payload.get("team_id") or "")


def slack_user_id(payload: dict[str, Any]) -> str:
    return str((payload.get("user") or {}).get("id") or payload.get("user_id") or "")


def ephemeral(text: str, **extra: Any) -> dict[str, Any]:
    """Slack shows this to the person who pressed the button and to nobody else."""
    return {"response_type": "ephemeral", "replace_original": False, "text": text, **extra}


# --- identity -------------------------------------------------------------------------------------


def resolve_actor(conn: psycopg.Connection, payload: dict[str, Any]) -> dict[str, Any]:
    """`{tenant_id, user_id}` for the press, or `{error}` — on a tenant-UNBOUND connection.

    Both lookups are SECURITY DEFINER functions that return one row and no credential: the workspace's
    installation and the Slack user's StartupOS identity. A user who belongs to a different tenant than the
    workspace is exactly as unauthorised as an unmapped one.
    """
    from auth import slack_install
    from daemon.gateway import slack as gateway

    installation = slack_install.lookup_team(conn, team_id(payload))
    if not installation or installation.get("revoked_at") is not None:
        return {"error": NOT_CONNECTED}
    mapped = gateway.lookup_slack_user(conn, slack_user_id(payload))
    if not mapped or mapped[0] != installation["tenant_id"]:
        log.info("slack interactivity from an unlinked user in team %s", team_id(payload))
        return {"error": gateway.link_hint()}
    return {"tenant_id": mapped[0], "user_id": mapped[1]}


# --- the decision ---------------------------------------------------------------------------------


def _decide(conn: psycopg.Connection, approval_id: str, action_id: str, user_id: str) -> bool:
    """Run the approval gate once. True if THIS press decided it; False if someone (or another click) got there.

    `approvals.decide` takes the row `FOR UPDATE`, so two simultaneous presses serialise: the loser sees a row
    that is no longer `pending` and gets a ValueError, which is not an error here — it is the no-op.
    """
    decision = (
        Decision(decision="approve", decided_by=user_id)
        if action_id == APPROVE_ACTION
        else Decision(decision="decline", reason=DECLINE_REASON, decided_by=user_id)
    )
    try:
        approvals.decide(conn, approval_id, decision)
    except ValueError:  # already decided between our read and the FOR UPDATE — harmless
        return False
    return True


def update_message(conn: psycopg.Connection, tenant_id: str, approval: Approval) -> dict[str, Any]:
    """Re-render the original Slack message as the decided approval. `{updated: bool, reason?|error?}`.

    Never raises: the decision is already in Postgres and must not be undone by a Slack problem.
    """
    target = approval_message(conn, tenant_id, approval.id)
    if not target:
        return {"updated": False, "reason": "no delivered message for this approval"}
    token = delivery.slack_token(conn, tenant_id)
    if not token:
        return {"updated": False, "reason": "no Slack credential for this tenant"}
    text, blocks = slack_blocks.approval_blocks(approval, decided=approval)
    try:
        slack_executor.update_message(
            {"channel": target["channel"], "ts": target["ts"], "text": text, "blocks": blocks}, token=token
        )
    except Exception as exc:  # a failed edit leaves stale buttons; the decision itself already stands
        message = f"{type(exc).__name__}: {str(exc)[:200]}"
        log.warning("chat.update for approval %s (%s) failed: %s", approval.id, tenant_id, message)
        return {"updated": False, "error": message}
    return {"updated": True, "channel": target["channel"], "ts": target["ts"]}


def approval_message(conn: psycopg.Connection, tenant_id: str, approval_id: str) -> dict[str, Any] | None:
    """`{channel, ts}` of the message carrying this approval's buttons, or None if it never landed.

    The deliveries ledger is the record of what was posted where (Track D writes `approval:<id>` with the `ts`).
    An approval proposed while Slack was disconnected has no message to update, and deciding it still works.
    """
    row = conn.execute(
        """SELECT channel, ts FROM deliveries
            WHERE tenant_id = %s AND kind = 'approval' AND ref = %s AND status = 'sent' AND ts IS NOT NULL""",
        (tenant_id, f"approval:{approval_id}"),
    ).fetchone()
    if not row or not row["ts"] or not row["channel"]:
        return None
    return {"channel": row["channel"], "ts": row["ts"]}


def act(conn: psycopg.Connection, tenant_id: str, user_id: str, action: dict[str, Any]) -> dict[str, Any]:
    """Decide (once), execute approvals only, re-render the message. The tenant-bound half of `handle_action`."""
    approval_id, action_id = action["approval_id"], action["action_id"]
    approval = approvals.get(conn, approval_id)
    if approval is None or approval.tenant_id != tenant_id:
        log.info("slack interactivity for an approval outside tenant %s", tenant_id)
        return ephemeral(GONE)

    decided_now = _decide(conn, approval_id, action_id, user_id) if approval.status == "pending" else False
    if decided_now and action_id == APPROVE_ACTION:
        # The gate, then the executor: run_approved re-reads the row FOR UPDATE, refuses anything not `approved`,
        # and resolves this tenant's own credential. An executor failure is a `failed` row, not an exception.
        executors.run_approved(conn, approval_id)

    final = approvals.get(conn, approval_id)
    assert final is not None
    message = update_message(conn, tenant_id, final)
    result = final.result or {}
    return {
        "ok": True,
        "approval": approval_id,
        "status": final.status,
        "decided": decided_now,
        "acted_by": final.decided_by,
        "message": message,
        **({"error": result["error"]} if result.get("error") else {}),
    }


def handle_action(payload: dict[str, Any], *, conn_factory: Any = None) -> dict[str, Any]:
    """Dispatch one interactivity payload. Raises UnknownAction for an action_id this module does not own."""
    action = approval_action(payload)  # before any I/O: an unknown action must touch nothing
    if conn_factory is None:
        raise RuntimeError("handle_action requires a conn_factory(tenant_id)")

    with conn_factory("") as anon:
        actor = resolve_actor(anon, payload)
    if actor.get("error"):
        return ephemeral(actor["error"])

    with conn_factory(actor["tenant_id"]) as conn:
        return act(conn, actor["tenant_id"], actor["user_id"], action)
