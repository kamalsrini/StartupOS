"""Slack request signing (Sprint 3b, Track I): accept the real shape, reject everything else.

Fixture shapes are Slack's own: `X-Slack-Request-Timestamp` seconds, `X-Slack-Signature: v0=<hex>`, HMAC-SHA256
over `v0:{ts}:{raw body}` — an Events API JSON body and an interactivity form body, both byte-for-byte as Slack
sends them.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from auth import slack_sig

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"  # Slack's own documentation sample secret
TS = 1531420618
# Verbatim Events API body (url_verification) and an interactivity form body.
EVENT_BODY = (
    b'{"token":"z26uFbvR1xHJEdHE1OQiO6t8","team_id":"T061EG9R6","api_app_id":"A0MDYCDME",'
    b'"event":{"type":"app_mention","user":"U061F7AUR","text":"<@U0LAN0Z89> pending","ts":"1515449522.000016",'
    b'"channel":"C0LAN2Q65","event_ts":"1515449522000016"},"type":"event_callback","event_id":"Ev0MDYGDKJ",'
    b'"event_time":1515449522000016}'
)
FORM_BODY = b"payload=%7B%22type%22%3A%22block_actions%22%2C%22team%22%3A%7B%22id%22%3A%22T061EG9R6%22%7D%7D"


def headers(body: bytes, *, secret: str = SECRET, ts: int = TS) -> dict[str, str]:
    return {
        "X-Slack-Request-Timestamp": str(ts),
        "X-Slack-Signature": slack_sig.sign(ts, body, secret),
        "Content-Type": "application/json",
    }


def test_signature_matches_slacks_own_construction():
    """Our `sign` is exactly HMAC-SHA256 over v0:{ts}:{body} — computed here independently of the module."""
    expected = "v0=" + hmac.new(SECRET.encode(), b"v0:%d:%s" % (TS, EVENT_BODY), hashlib.sha256).hexdigest()
    assert slack_sig.sign(TS, EVENT_BODY, SECRET) == expected
    assert slack_sig.verify(headers(EVENT_BODY), EVENT_BODY, SECRET, now=TS + 5) is True


@pytest.mark.parametrize("body", [EVENT_BODY, FORM_BODY, b"", b'{"unicode":"caf\\u00e9 \xe2\x9c\x93"}'])
def test_accepts_every_body_shape_slack_sends(body):
    assert slack_sig.verify(headers(body), body, SECRET, now=TS + 1) is True


def test_tampered_body_is_rejected():
    h = headers(EVENT_BODY)
    tampered = EVENT_BODY.replace(b"> pending", b"> approve uni-1")  # the attacker upgrades a read to a write
    assert tampered != EVENT_BODY
    with pytest.raises(slack_sig.SlackSignatureError, match="does not match"):
        slack_sig.verify(h, tampered, SECRET, now=TS + 1)
    # one flipped byte is enough
    with pytest.raises(slack_sig.SlackSignatureError):
        slack_sig.verify(h, EVENT_BODY + b" ", SECRET, now=TS + 1)


def test_wrong_secret_is_rejected():
    with pytest.raises(slack_sig.SlackSignatureError, match="does not match"):
        slack_sig.verify(headers(EVENT_BODY), EVENT_BODY, SECRET + "x", now=TS + 1)


def test_replay_window_is_five_minutes():
    h = headers(EVENT_BODY)
    assert slack_sig.MAX_AGE_SECONDS == 300
    assert slack_sig.verify(h, EVENT_BODY, SECRET, now=TS + 299) is True
    with pytest.raises(slack_sig.SlackSignatureError, match="replay window"):
        slack_sig.verify(h, EVENT_BODY, SECRET, now=TS + 301)
    # a replayed request with a *future* timestamp is just as bad (clock-skew attack)
    with pytest.raises(slack_sig.SlackSignatureError, match="replay window"):
        slack_sig.verify(h, EVENT_BODY, SECRET, now=TS - 600)


@pytest.mark.parametrize(
    "hdrs",
    [
        {},
        {"X-Slack-Request-Timestamp": str(TS)},  # no signature
        {"X-Slack-Signature": slack_sig.sign(TS, EVENT_BODY, SECRET)},  # no timestamp
        {"X-Slack-Request-Timestamp": "", "X-Slack-Signature": ""},
    ],
)
def test_missing_headers_are_rejected(hdrs):
    with pytest.raises(slack_sig.SlackSignatureError, match="missing"):
        slack_sig.verify(hdrs, EVENT_BODY, SECRET, now=TS)


def test_malformed_headers_are_rejected():
    with pytest.raises(slack_sig.SlackSignatureError, match="malformed Slack timestamp"):
        slack_sig.verify(
            {"X-Slack-Request-Timestamp": "not-a-number", "X-Slack-Signature": "v0=deadbeef"},
            EVENT_BODY,
            SECRET,
            now=TS,
        )
    with pytest.raises(slack_sig.SlackSignatureError, match="malformed Slack signature"):
        slack_sig.verify(
            {"X-Slack-Request-Timestamp": str(TS), "X-Slack-Signature": "v9=deadbeef"}, EVENT_BODY, SECRET, now=TS
        )


def test_no_signing_secret_is_a_distinct_failure():
    """Unconfigured is 503, not 403: the route must not read as 'that request was forged'."""
    with pytest.raises(slack_sig.SlackSigningNotConfigured):
        slack_sig.verify(headers(EVENT_BODY), EVENT_BODY, None, now=TS)
    with pytest.raises(slack_sig.SlackSigningNotConfigured):
        slack_sig.verify(headers(EVENT_BODY), EVENT_BODY, "", now=TS)
    assert issubclass(slack_sig.SlackSigningNotConfigured, slack_sig.SlackSignatureError)


def test_headers_are_read_case_insensitively():
    """Starlette lowercases; a plain dict from a test or a proxy may not."""
    sig = slack_sig.sign(TS, EVENT_BODY, SECRET)
    for key_ts, key_sig in (
        ("x-slack-request-timestamp", "x-slack-signature"),
        ("X-Slack-Request-Timestamp", "X-Slack-Signature"),
        ("X-SLACK-REQUEST-TIMESTAMP", "X-SLACK-SIGNATURE"),
    ):
        assert slack_sig.verify({key_ts: str(TS), key_sig: sig}, EVENT_BODY, SECRET, now=TS) is True


def test_str_body_and_bytes_body_sign_the_same():
    assert slack_sig.sign(TS, EVENT_BODY, SECRET) == slack_sig.sign(TS, EVENT_BODY.decode(), SECRET)
