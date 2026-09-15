"""In-process attempt limiters for the handful of routes a stranger can hammer.

Sprint 3d (PE review): the deployment moved from a VM with every port closed to one public HTTPS hostname with
self-serve Google sign-up, so "only we can reach it" stopped being a control. Two things had to change.

1. **The key has to be the real client.** `request.client.host` is the *proxy's* address behind Caddy, so an
   IP-keyed limiter degrades into one global bucket shared by the whole internet — it throttles everybody at
   once and nobody in particular. Caddy APPENDS the peer it actually accepted the connection from to
   `X-Forwarded-For`, so the LAST element of that header is the one value a client cannot forge (anything it
   sends is kept to the left of Caddy's entry). We use that, and fall back to the socket address when the header
   is absent (local dev, tests, a direct call on the loopback publish).

2. **Every route that checks a shared secret or creates state has to be limited, not just one of them.**
   `POST /auth/bootstrap` was throttled; `POST /onboarding/tenant` takes the *same* bootstrap token and was not,
   so the throttle was one route wide and the brute force simply moved next door.

Limits are per API process, which is what this deployment is (one uvicorn container). They are a speed bump for
online guessing and automated abuse, not a distributed quota; `docs/HOSTING.md` says so.
"""

from __future__ import annotations

import time
from typing import Any

# Every Throttle registers itself so tests can clear the state between cases (rate limiting is a property of the
# deployment, not of the case under test) and so an operator has one place to look.
_REGISTRY: list[Throttle] = []


class Throttle:
    """`limit` attempts per `window_s` seconds per key. Not a quota: state is per process and per key."""

    def __init__(self, limit: int = 5, window_s: int = 60, *, name: str = "") -> None:
        self.limit, self.window, self.name = limit, window_s, name
        self._hits: dict[str, list[float]] = {}
        _REGISTRY.append(self)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if len(hits) >= self.limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def reset(self) -> None:
        self._hits.clear()


def reset_all() -> None:
    """Forget every recorded attempt. For tests only."""
    for throttle in _REGISTRY:
        throttle.reset()


def client_key(request: Any) -> str:
    """Who is calling, for rate-limiting purposes: the proxy's own X-Forwarded-For entry, else the socket peer.

    The LAST X-Forwarded-For element is the address the trusted proxy in front of us accepted the connection
    from. Taking the first would let anyone mint a fresh bucket per request with one header.
    """
    forwarded = request.headers.get("x-forwarded-for") if hasattr(request, "headers") else None
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1][:64]
    client = getattr(request, "client", None)
    return (getattr(client, "host", None) or "?")[:64]
