"""api/ratelimit.py — the limiter the public routes share, and the key it counts against.

The key is the interesting half. Behind Caddy `request.client.host` is the proxy's own address for every
request in the world, so an IP-keyed limiter silently becomes one global bucket: it throttles everybody at once
and nobody in particular. Caddy APPENDS the peer it accepted the connection from to X-Forwarded-For, so the last
element is the one a caller cannot choose; the elements to its left are whatever the caller sent.
"""

from __future__ import annotations

import pytest

from api import ratelimit


class FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class FakeRequest:
    def __init__(self, headers: dict[str, str] | None = None, host: str | None = "127.0.0.1") -> None:
        self.headers = headers or {}
        self.client = FakeClient(host) if host else None


@pytest.fixture(autouse=True)
def _clean():
    ratelimit.reset_all()
    yield
    ratelimit.reset_all()


def test_a_throttle_allows_up_to_the_limit_then_refuses():
    t = ratelimit.Throttle(limit=3, window_s=60, name="test.limit")
    assert [t.allow("a") for _ in range(4)] == [True, True, True, False]
    assert t.allow("b") is True  # a different key has its own budget
    t.reset()
    assert t.allow("a") is True


def test_a_refused_attempt_does_not_extend_the_window():
    """Hammering a closed door must not keep it closed: the refusal is not recorded as an attempt."""
    t = ratelimit.Throttle(limit=1, window_s=60, name="test.window")
    assert t.allow("a") is True
    for _ in range(5):
        assert t.allow("a") is False
    assert t._hits["a"] == t._hits["a"][:1]  # still exactly the one real attempt


def test_the_window_expires():
    t = ratelimit.Throttle(limit=1, window_s=0, name="test.expiry")
    assert t.allow("a") is True
    assert t.allow("a") is True  # a zero-length window forgets immediately


def test_the_key_is_the_entry_the_proxy_appended_not_the_one_the_caller_sent():
    # Caddy appends the real peer last; everything to the left of it came from the client.
    req = FakeRequest({"x-forwarded-for": "1.2.3.4, 9.9.9.9, 203.0.113.7"})
    assert ratelimit.client_key(req) == "203.0.113.7"
    # So a caller rotating forged entries cannot mint a fresh bucket per request.
    keys = {ratelimit.client_key(FakeRequest({"x-forwarded-for": f"10.0.0.{i}, 203.0.113.7"})) for i in range(10)}
    assert keys == {"203.0.113.7"}


def test_the_key_falls_back_to_the_socket_and_is_always_a_bounded_string():
    assert ratelimit.client_key(FakeRequest(host="198.51.100.4")) == "198.51.100.4"
    assert ratelimit.client_key(FakeRequest(host=None)) == "?"
    assert ratelimit.client_key(FakeRequest({"x-forwarded-for": "  "}, host="198.51.100.4")) == "198.51.100.4"
    long = ratelimit.client_key(FakeRequest({"x-forwarded-for": "x" * 500}))
    assert len(long) == 64  # a header cannot become an unbounded dictionary key


def test_reset_all_reaches_the_limiters_the_routers_own():
    """The registry is what lets tests clear production rate limiting; if a limiter escapes it, tests go flaky."""
    import api.main  # noqa: F401  - imports every router, so every module-level Throttle exists
    from api.routers import auth as auth_router
    from api.routers import onboarding as onboarding_router

    names = {t.name for t in ratelimit._REGISTRY}
    assert {"auth.bootstrap", "onboarding.tenant"} <= names
    for throttle in (auth_router._bootstrap_throttle, onboarding_router._signup_throttle):
        assert throttle.allow("k") is True
    ratelimit.reset_all()
    assert all(not t._hits for t in ratelimit._REGISTRY)
