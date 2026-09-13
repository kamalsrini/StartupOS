"""Pure Block Kit renderers for everything StartupOS posts to Slack. No I/O, no DB, no settings.

Every renderer returns `(fallback_text, blocks)`:

  * `fallback_text` is the `text` argument of `chat.postMessage` — it is what a screen reader reads, what a
    push notification shows and what a client that cannot render blocks falls back to. It is NEVER empty.
  * `blocks` is a list of Block Kit dicts. Slack rejects a message whose block text exceeds 3000 characters,
    so long content is split across sections (and, past `MAX_BLOCKS`, truncated with an ellipsis) rather than
    sent and rejected. Counting is by character, which is what Slack counts, so emoji and CJK are safe.

`approval_blocks` renders the Approve/Decline buttons Track B's `daemon/slack_actions.py` dispatches on:
`action_id` is `approval_approve` / `approval_decline` and `value` is the approval id. With `decided=` it
renders the resolved state and NO buttons, which is what `chat.update` posts over the original message so a
decision cannot be pressed twice.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# Slack limits (https://api.slack.com/reference/block-kit). We stay strictly under them.
MAX_TEXT = 3000
MAX_HEADER = 150
MAX_BUTTON_TEXT = 75
MAX_VALUE = 2000
MAX_ACTION_ID = 255
MAX_BLOCKS = 50
MAX_FALLBACK = 4000

APPROVE_ACTION = "approval_approve"
DECLINE_ACTION = "approval_decline"

SEVERITY_EMOJI = {"high": "🔴", "medium": "🟠", "low": "🟡", "info": "🔵"}
DECIDED_EMOJI = {"approved": "✅", "executed": "✅", "declined": "🚫", "failed": "⚠️"}


# --- primitives ---------------------------------------------------------------------------------


def truncate(text: str, limit: int = MAX_TEXT) -> str:
    """Cut to `limit` characters, keeping the cut visible. Never returns more than `limit` characters."""
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def split_text(text: str, limit: int = MAX_TEXT, max_parts: int = 8) -> list[str]:
    """Split long text into <= `limit`-character chunks, preferring line boundaries.

    The founder's pulse is not worth truncating at 3000 characters when it can be two sections; past
    `max_parts` chunks we do truncate, because a Slack message is capped at 50 blocks anyway.
    """
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    rest = text
    while rest and len(parts) < max_parts:
        if len(rest) <= limit:
            parts.append(rest)
            rest = ""
            break
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts[-1] = truncate(parts[-1] + "\n…", limit)
    return [p for p in parts if p] or [""]


def section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": truncate(text, MAX_TEXT) or " "}}


def sections(text: str) -> list[dict[str, Any]]:
    return [section(part) for part in split_text(text)]


def header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": truncate(text, MAX_HEADER) or " ", "emoji": True}}


def context(*lines: str) -> dict[str, Any]:
    kept = [truncate(line, MAX_TEXT) for line in lines if line]
    if not kept:
        kept = [" "]
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": line} for line in kept[:10]]}


def divider() -> dict[str, Any]:
    return {"type": "divider"}


def button(text: str, action_id: str, value: str, *, style: str | None = None) -> dict[str, Any]:
    el: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": truncate(text, MAX_BUTTON_TEXT) or " ", "emoji": True},
        "action_id": truncate(action_id, MAX_ACTION_ID),
        "value": truncate(str(value), MAX_VALUE) or "-",
    }
    if style:
        el["style"] = style
    return el


def link(url: str | None, label: str) -> str:
    """A Slack mrkdwn link, or plain text when there is no URL."""
    return f"<{url}|{label}>" if url else label


def fallback(text: str, default: str) -> str:
    """First non-empty line of `text`, or `default`. Never empty — notifications and screen readers need it."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return truncate(line, MAX_FALLBACK)
    return truncate(default, MAX_FALLBACK)


def finish(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enforce the 50-block cap, keeping a marker so nothing disappears silently."""
    if len(blocks) <= MAX_BLOCKS:
        return blocks
    return [*blocks[: MAX_BLOCKS - 1], context("… truncated")]


def validate(blocks: list[dict[str, Any]]) -> list[str]:
    """Problems that would make Slack reject the message. Empty list = valid. Used by the tests and by delivery."""
    problems: list[str] = []
    if not isinstance(blocks, list) or not blocks:
        return ["blocks must be a non-empty list"]
    if len(blocks) > MAX_BLOCKS:
        problems.append(f"{len(blocks)} blocks > {MAX_BLOCKS}")
    for i, block in enumerate(blocks):
        if not isinstance(block, dict) or not block.get("type"):
            problems.append(f"block {i}: missing type")
            continue
        for label, obj in _texts(block):
            if len(obj.get("text", "")) > MAX_TEXT:
                problems.append(f"block {i} ({block['type']}.{label}): {len(obj['text'])} chars > {MAX_TEXT}")
            if obj.get("type") == "plain_text" and block["type"] == "header" and len(obj.get("text", "")) > MAX_HEADER:
                problems.append(f"block {i} (header): {len(obj['text'])} chars > {MAX_HEADER}")
        for el in block.get("elements", []) if block["type"] == "actions" else []:
            if el.get("type") == "button":
                if not el.get("action_id"):
                    problems.append(f"block {i}: button without action_id")
                if not el.get("value"):
                    problems.append(f"block {i}: button without value")
    return problems


def _texts(block: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    if isinstance(block.get("text"), dict):
        out.append(("text", block["text"]))
    for f in block.get("fields") or []:
        if isinstance(f, dict):
            out.append(("field", f))
    if block.get("type") == "context":
        for el in block.get("elements") or []:
            if isinstance(el, dict) and el.get("type") in ("mrkdwn", "plain_text"):
                out.append(("element", el))
    return out


def _day(value: Any) -> str:
    if isinstance(value, datetime | date):
        return value.strftime("%a %b %d, %Y")
    return str(value) if value else ""


# --- renderers ----------------------------------------------------------------------------------


def pulse_blocks(
    text: str,
    tiles: list[dict[str, Any]] | None = None,
    approvals_count: int = 0,
    web_url: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """The 07:00 morning pulse: the model's four lines + three things, plus Tier-0 tiles and the approval count."""
    blocks: list[dict[str, Any]] = [header("Morning pulse")]
    blocks.extend(sections(text or "_no pulse text_"))
    tile_line = " · ".join(
        f"*{t.get('val', '')}* {t.get('label', '')}".strip() for t in (tiles or []) if t.get("label") or t.get("val")
    )
    if tile_line:
        blocks.append(context(tile_line))
    waiting = f"{approvals_count} approval{'' if approvals_count == 1 else 's'} waiting"
    blocks.append(context(f"{waiting} · {link(web_url, 'Open the cockpit')}"))
    return fallback(text, f"Morning pulse — {waiting}"), finish(blocks)


def digest_blocks(data: dict[str, Any] | str) -> tuple[str, list[dict[str, Any]]]:
    """The 18:00 evening digest. `data` is `{text, date?, approvals_pending?, open_signals?, web_url?}`
    (a plain string is accepted and treated as `{text: ...}`)."""
    data = {"text": data} if isinstance(data, str) else dict(data or {})
    text = str(data.get("text") or "")
    day = _day(data.get("date") or data.get("now"))
    blocks: list[dict[str, Any]] = [header(f"Evening digest · {day}" if day else "Evening digest")]
    blocks.extend(sections(text or "_nothing to report_"))
    bits: list[str] = []
    if data.get("approvals_pending") is not None:
        bits.append(f"{data['approvals_pending']} approvals waiting")
    if data.get("open_signals") is not None:
        bits.append(f"{data['open_signals']} signals open")
    bits.append(link(data.get("web_url"), "Open the cockpit"))
    blocks.append(context(" · ".join(bits)))
    return fallback(text, f"Evening digest{f' · {day}' if day else ''}"), finish(blocks)


def signal_blocks(signal: dict[str, Any], *, overflow: int = 0) -> tuple[str, list[dict[str, Any]]]:
    """One high-severity (or Chief of Staff) signal. `overflow` > 0 appends the "+N more" line to this message."""
    s = dict(signal or {})
    severity = str(s.get("severity") or "info")
    module = str(s.get("module") or "cockpit")
    title = str(s.get("title") or s.get("id") or "signal")
    meta = str(s.get("meta") or "")
    emoji = SEVERITY_EMOJI.get(severity, "•")
    body = f"{emoji} *{link(s.get('href'), title)}*"
    if meta:
        body += f"\n{meta}"
    blocks: list[dict[str, Any]] = [*sections(body)]
    trail = f"{module} · {severity}"
    if s.get("suggested_skill"):
        trail += f" · suggested: `{s['suggested_skill']}`"
    if overflow > 0:
        trail += f" · +{overflow} more waiting in the cockpit"
    blocks.append(context(trail))
    return fallback(f"[{severity}] {module}: {title}", f"{module} signal"), finish(blocks)


def approval_blocks(approval: Any, *, decided: Any = None) -> tuple[str, list[dict[str, Any]]]:
    """An approval with Approve/Decline buttons, or — with `decided` — the resolved message that replaces it.

    `decided` is the decided approval (a row/dict/model with `status`, `decided_by`, `decline_reason`, `result`)
    or simply the status string. Buttons are omitted whenever `decided` is given, so a decision cannot be
    pressed twice.
    """
    a = _as_dict(approval)
    aid = str(a.get("id") or "")
    title = f"*{a.get('type') or 'Approval'}* · {a.get('target') or aid}"
    blocks: list[dict[str, Any]] = [*sections(f"{title}\n{a.get('preview') or ''}".strip())]
    if decided is None:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"approval:{truncate(aid, 200)}",
                "elements": [
                    button("Approve", APPROVE_ACTION, aid, style="primary"),
                    button("Decline", DECLINE_ACTION, aid, style="danger"),
                ],
            }
        )
        blocks.append(context(f"{a.get('module') or 'cockpit'} · approval `{aid}`"))
        text = fallback(f"Approval needed: {a.get('type') or ''} {a.get('target') or aid}".strip(), "Approval needed")
        return text, finish(blocks)

    d = _as_dict(decided) if not isinstance(decided, str) else {"status": decided}
    status = str(d.get("status") or "decided")
    who = d.get("decided_by") or a.get("decided_by")
    line = f"{DECIDED_EMOJI.get(status, '•')} {status.capitalize()}"
    if who:
        line += f" by {who}"
    if d.get("decline_reason"):
        line += f" — {d['decline_reason']}"
    result = d.get("result") or {}
    if isinstance(result, dict) and result.get("error"):
        line += f" — execution failed: {result['error']}"
    elif isinstance(result, dict) and result.get("text"):
        line += f" — {result['text']}"
    blocks.append(context(line))
    return fallback(f"{status.capitalize()}: {a.get('type') or ''} {a.get('target') or aid}".strip(), "Approval"), (
        finish(blocks)
    )


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return dict(obj)
