"""Slack executor: `Slack.post_message` → chat.postMessage via slack_sdk.

Input: {channel, text, thread_ts?}. Token only via settings.secret("env:SLACK_BOT_TOKEN"). Tests inject `_client_factory`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from common.settings import settings


class SlackError(RuntimeError):
    pass


def _default_client_factory() -> Any:
    token = settings.secret("env:SLACK_BOT_TOKEN")
    if not token:
        raise SlackError("SLACK_BOT_TOKEN is not configured")
    from slack_sdk import WebClient

    return WebClient(token=token)


_client_factory: Callable[[], Any] = _default_client_factory


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


def post_message(inp: dict[str, Any], *, client: Any | None = None) -> dict[str, Any]:
    channel = inp.get("channel")
    text = inp.get("text")
    if not channel or not text:
        raise SlackError("post_message requires `channel` and `text`")
    client = client or _client_factory()
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
