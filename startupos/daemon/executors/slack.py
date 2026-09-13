"""Slack executor: `Slack.post_message` → chat.postMessage via slack_sdk.

Input: {channel, text, thread_ts?}. The bot token is an argument (`token`), resolved per tenant by daemon/executors
from the tenant's `connections` row; this module never reads the environment. Tests inject `_client_factory`.
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
    if inp.get("thread_ts"):
        kwargs["thread_ts"] = inp["thread_ts"]
    resp = client.chat_postMessage(**kwargs)
    ok = resp.get("ok", True) if hasattr(resp, "get") else True
    if not ok:
        raise SlackError(str(resp.get("error", "chat.postMessage failed")))
    ts = resp.get("ts", "") if hasattr(resp, "get") else ""
    posted_channel = resp.get("channel", channel) if hasattr(resp, "get") else channel
    return {"text": f"Posted to {posted_channel}", "url": _permalink(client, posted_channel, ts)}
