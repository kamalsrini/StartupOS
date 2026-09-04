"""Session cookie signing: sign/verify, tamper, expiry, secret handling."""

from __future__ import annotations

import time

import pytest

from auth import config, sessions


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", "unit-test-secret")
    monkeypatch.delenv("STARTUPOS_DEV", raising=False)


def test_sign_then_verify_roundtrip():
    sid = "abc123-session"
    cookie = sessions.sign_session_id(sid)
    assert cookie != sid and sid in cookie
    assert sessions.verify_cookie(cookie) == sid


def test_tampered_cookie_rejected():
    cookie = sessions.sign_session_id("real-session")
    assert sessions.verify_cookie(cookie.replace("real", "fake")) is None
    assert sessions.verify_cookie(cookie[:-3] + "xyz") is None
    assert sessions.verify_cookie("") is None
    assert sessions.verify_cookie(None) is None
    assert sessions.verify_cookie("real-session") is None  # unsigned


def test_cookie_signed_with_other_secret_rejected(monkeypatch):
    cookie = sessions.sign_session_id("s1")
    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", "another-secret")
    assert sessions.verify_cookie(cookie) is None


def test_expired_cookie_rejected():
    from itsdangerous import TimestampSigner

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(TimestampSigner, "get_timestamp", lambda self: int(time.time()) - 100)
        cookie = sessions.sign_session_id("s1")  # signed "100 seconds ago"
    assert sessions.verify_cookie(cookie, max_age=30) is None
    assert sessions.verify_cookie(cookie, max_age=600) == "s1"


def test_missing_secret_raises_unless_dev(monkeypatch):
    monkeypatch.delenv("STARTUPOS_SESSION_SECRET")
    with pytest.raises(RuntimeError):
        config.session_secret()
    monkeypatch.setenv("STARTUPOS_DEV", "1")
    assert config.session_secret() == config.DEV_INSECURE_SECRET


def test_ttl_from_env(monkeypatch):
    monkeypatch.setenv("STARTUPOS_SESSION_TTL_HOURS", "2")
    assert sessions.ttl_seconds() == 7200
    monkeypatch.setenv("STARTUPOS_SESSION_TTL_HOURS", "nope")
    assert sessions.ttl_seconds() == 336 * 3600
