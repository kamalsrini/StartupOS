"""Slack executor: `Slack.post_message` → chat.postMessage via slack_sdk, plus `update_message` → chat.update.

Input: {channel, text, thread_ts?}. The bot token is an argument (`token`), resolved per tenant by daemon/executors
from the tenant's `connections` row; this module never reads the environment. Tests inject `_client_factory`.

`update_message` (Sprint 3b, Track B) edits a message StartupOS already posted — an approval whose buttons were
just pressed. It is NOT in the executors allow-list: approvals never *ask* for a chat.update, it is how the
approval gate tells the founder what it did. It lives here because the executor stays the only Slack write path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class SlackError(RuntimeError):
    pass


def _default_client_factory(token: str | None = None) -> Any:
    if not token:
        raise SlackError("no Slack credential for this tenant (connect Slack in onboarding)")
    from slack_sdk import WebClient

    return WebClient(token=token)


_client_factory: Callable[..., Any] = _default_client_factory


def _client(token: str | None) -> Any:
    """An injected `_client_factory` (tests) as is; the real one bound to this tenant's token."""
    if _client_factory is _default_client_factory:
        return _default_client_factory(token)
    return _client_factory()


def _permalink(client: Any, channel: str, ts: str) -> str | None:
    try:
        resp = client.chat_getPermalink(channel=channel, message_ts=ts)
        link = resp.get("permalink") if hasattr(resp, "get") else None
        if link:
            return link
    except Exception:  # noqa: S110 - permalink is best effort; fall through to the archive URL
        pass
    if channel and ts:
        return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"
    return None


def post_message(inp: dict[str, Any], *, client: Any | None = None, token: str | None = None) -> dict[str, Any]:
    channel = inp.get("channel")
    text = inp.get("text")
    if not channel or not text:
        raise SlackError("post_message requires `channel` and `text`")
    client = client or _client(token)
    kwargs: dict[str, Any] = {"channel": channel, "text": text}
    # Sprint 3b (Track D): Block Kit is optional and additive — `text` stays the required notification/screen-reader
    # fallback, so every existing caller (and every client that cannot render blocks) is unaffected.
    if inp.get("blocks"):
        kwargs["blocks"] = inp["blocks"]
    if inp.get("thread_ts"):
        kwargs["thread_ts"] = inp["thread_ts"]
    resp = client.chat_postMessage(**kwargs)
    ok = resp.get("ok", True) if hasattr(resp, "get") else True
    if not ok:
        raise SlackError(str(resp.get("error", "chat.postMessage failed")))
    ts = resp.get("ts", "") if hasattr(resp, "get") else ""
    posted_channel = resp.get("channel", channel) if hasattr(resp, "get") else channel
    # `ts` and `channel` are returned too: deliveries records them, and Track B's chat.update needs both.
    return {
        "text": f"Posted to {posted_channel}",
        "url": _permalink(client, posted_channel, ts),
        "ts": ts,
        "channel": posted_channel,
    }


def update_message(inp: dict[str, Any], *, client: Any | None = None, token: str | None = None) -> dict[str, Any]:
    """Replace a message in place: {channel, ts, text, blocks?} → chat.update.

    Same shape and same rules as `post_message` (token argument, injectable client, never the environment). `text`
    stays the required fallback so notifications and screen readers read the new state, not the old one.
    """
    channel, ts, text = inp.get("channel"), inp.get("ts"), inp.get("text")
    if not channel or not ts or not text:
        raise SlackError("update_message requires `channel`, `ts` and `text`")
    client = client or _client(token)
    kwargs: dict[str, Any] = {"channel": channel, "ts": ts, "text": text}
    if inp.get("blocks") is not None:
        kwargs["blocks"] = inp["blocks"]
    resp = client.chat_update(**kwargs)
    ok = resp.get("ok", True) if hasattr(resp, "get") else True
    if not ok:
        raise SlackError(str(resp.get("error", "chat.update failed")))
    return {
        "text": f"Updated {channel}",
        "ts": (resp.get("ts", ts) if hasattr(resp, "get") else ts),
        "channel": (resp.get("channel", channel) if hasattr(resp, "get") else channel),
    }
