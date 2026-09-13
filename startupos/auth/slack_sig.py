"""Slack request signing (Sprint 3b, Track I).

Every inbound Slack HTTP request (`/slack/events`, `/slack/interactivity`) carries two headers:

    X-Slack-Request-Timestamp: 1758000000
    X-Slack-Signature:         v0=<hex hmac-sha256>

The signature is HMAC-SHA256 over the literal basestring `v0:{timestamp}:{raw body}` keyed with the app's
signing secret (`STARTUPOS_SLACK_SIGNING_SECRET`). The raw body matters: re-serialising JSON or re-encoding a
form body changes the bytes and the signature will not match — the routers pass `await request.body()` straight
through.

Rules, all of them fail closed:
  * missing or malformed header  → reject (never "no signature, must be a test")
  * timestamp older (or newer) than MAX_AGE_SECONDS → reject; this is the replay window Slack documents
  * signature compared with `hmac.compare_digest` → no timing oracle on the secret
  * no signing secret configured → reject with a distinct reason so the route can answer 503 instead of 403

Nothing here logs a body, a signature or the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

TIMESTAMP_HEADER = "x-slack-request-timestamp"
SIGNATURE_HEADER = "x-slack-signature"
VERSION = "v0"
MAX_AGE_SECONDS = 300  # Slack's documented replay window


class SlackSignatureError(Exception):
    """An inbound request did not authenticate. The message is a short reason, never a value."""


class SlackSigningNotConfigured(SlackSignatureError):
    """STARTUPOS_SLACK_SIGNING_SECRET is not set: the route answers 503, not 403 — nothing is verifiable."""


def _header(headers: Any, name: str) -> str | None:
    """Case-insensitive header read that works for a dict, a Starlette Headers or a list of pairs."""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is None:
            value = getter(name.title())
        if value is None:
            value = getter(name.upper())
        return value
    for k, v in headers:  # pragma: no cover - iterable of pairs is a convenience only
        if str(k).lower() == name:
            return v
    return None


def basestring_for(timestamp: str, body: bytes | str) -> bytes:
    raw = body.encode() if isinstance(body, str) else bytes(body or b"")
    return b"%s:%s:%s" % (VERSION.encode(), str(timestamp).encode(), raw)


def sign(timestamp: str | int, body: bytes | str, signing_secret: str) -> str:
    """The `v0=…` header value Slack would send. Used by the routers' own tests and by nothing in production."""
    digest = hmac.new(signing_secret.encode(), basestring_for(str(timestamp), body), hashlib.sha256).hexdigest()
    return f"{VERSION}={digest}"


def verify(headers: Any, body: bytes | str, signing_secret: str | None, *, now: float | None = None) -> bool:
    """True when the request authenticates; raises SlackSignatureError with a short reason otherwise.

    `now` is injectable so the replay window is testable without sleeping.
    """
    if not signing_secret:
        raise SlackSigningNotConfigured("STARTUPOS_SLACK_SIGNING_SECRET is not configured")
    raw_ts = _header(headers, TIMESTAMP_HEADER)
    signature = _header(headers, SIGNATURE_HEADER)
    if not raw_ts or not signature:
        raise SlackSignatureError("missing Slack signature headers")
    try:
        ts = int(str(raw_ts).strip())
    except (TypeError, ValueError):
        raise SlackSignatureError("malformed Slack timestamp header") from None
    age = (time.time() if now is None else now) - ts
    if abs(age) > MAX_AGE_SECONDS:
        raise SlackSignatureError("Slack timestamp outside the replay window")
    signature = str(signature).strip()
    if not signature.startswith(f"{VERSION}="):
        raise SlackSignatureError("malformed Slack signature header")
    expected = sign(ts, body, signing_secret)
    if not hmac.compare_digest(expected, signature):
        raise SlackSignatureError("Slack signature does not match")
    return True
