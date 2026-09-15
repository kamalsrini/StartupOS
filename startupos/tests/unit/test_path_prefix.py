"""Track H — STARTUPOS_PATH_PREFIX (CONTRACTS.md Sprint 3d).

The deployed topology is one hostname: Caddy serves the Next.js app at `/` and hands `/api/...` to this API
*unchanged*. So the API has to own the prefix — every route and, the part that silently breaks OAuth, every
Set-Cookie Path.

Two properties are proved here:
  1. With the prefix empty (local dev, CI, and every other test in this suite) nothing moves at all.
  2. With the prefix set, routes AND cookie paths AND the Google/Slack URLs all land under it.

The prefix is read once when api.main is imported, so the prefixed case reloads the module inside a fixture
that puts it back afterwards.
"""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from auth import config

TEST_SECRET = "unit-test-session-secret-not-a-real-one"  # noqa: S105 - test fixture


@contextmanager
def api_at(prefix: str, *, docs: str = "1"):
    """Reload api.main with STARTUPOS_PATH_PREFIX=prefix, then put the module back as the suite found it.

    A non-empty prefix also means "this deployment is public" (auth.config.public_deployment), which switches
    the API docs off — a different property, tested in tests/unit/test_public_exposure.py. These tests are
    about where routes are MOUNTED, so they force the docs on and assert they move under the prefix like
    everything else.
    """
    import api.main

    before = {k: os.environ.get(k) for k in ("STARTUPOS_PATH_PREFIX", "STARTUPOS_ENABLE_DOCS")}
    os.environ["STARTUPOS_PATH_PREFIX"] = prefix
    os.environ["STARTUPOS_ENABLE_DOCS"] = docs
    try:
        yield importlib.reload(api.main)
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(api.main)


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", TEST_SECRET)
    monkeypatch.setenv("STARTUPOS_API_DSN", "postgresql://unused@127.0.0.1:1/none")
    monkeypatch.delenv("STARTUPOS_PATH_PREFIX", raising=False)
    monkeypatch.delenv("STARTUPOS_DOMAIN", raising=False)
    return monkeypatch


# --- normalisation -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("/", ""),
        ("  ", ""),
        ("/api", "/api"),
        ("api", "/api"),  # a missing leading slash is an easy .env typo, not a different prefix
        ("/api/", "/api"),
        ("/v1/api//", "/v1/api"),
    ],
)
def test_path_prefix_normalises(env, raw, expected):
    if raw is None:
        env.delenv("STARTUPOS_PATH_PREFIX", raising=False)
    else:
        env.setenv("STARTUPOS_PATH_PREFIX", raw)
    assert config.path_prefix() == expected


def test_prefixed_and_cookie_path_are_identity_when_empty(env):
    for path in ("/", "/health", "/auth/google", "/slack/events"):
        assert config.prefixed(path) == path
        assert config.cookie_path(path) == path


def test_prefixed_and_cookie_path_under_a_prefix(env):
    env.setenv("STARTUPOS_PATH_PREFIX", "/api")
    assert config.prefixed("/health") == "/api/health"
    assert config.prefixed("/") == "/api/"  # the mounted root keeps its slash
    assert config.cookie_path("/auth/google") == "/api/auth/google"
    # A Path of "/api" path-matches "/api" and everything below it (RFC 6265 §5.1.4), which is what we want
    # for the session cookie: one cookie for the whole API, sent to nothing else on the hostname.
    assert config.cookie_path("/") == "/api"


# --- routes ------------------------------------------------------------------------


def _mounted_paths(main) -> set[str]:
    """Fully-resolved route paths. FastAPI keeps the include_router prefix on the wrapper rather than rewriting
    each sub-route's .path, so walking app.routes would report the unprefixed paths; the OpenAPI document is
    where the paths the server actually matches are resolved."""
    return set(main.app.openapi()["paths"]) | {r.path for r in main.app.routes if getattr(r, "path", None)}


def test_routes_do_not_move_when_the_prefix_is_empty(env):
    """The regression guard: with no prefix the mounted paths are exactly the pre-Sprint-3d ones."""
    with api_at("") as main:
        paths = _mounted_paths(main)
    assert {"/", "/health", "/auth/google", "/auth/google/callback", "/cockpit", "/slack/events"} <= paths
    assert main.PUBLIC_PATHS == main._BASE_PUBLIC_PATHS
    assert main.PATH_PREFIX == ""
    assert main.app.docs_url == "/docs" and main.app.openapi_url == "/openapi.json"


def test_every_route_moves_under_the_prefix(env):
    with api_at("/api") as main:
        paths = _mounted_paths(main)
        assert {"/api/", "/api/health", "/api/auth/google", "/api/cockpit", "/api/slack/events"} <= paths
        # Nothing is left behind at the root — a half-moved API would answer some routes and 404 the rest.
        assert not [p for p in paths if p in ("/health", "/auth/google", "/cockpit", "/slack/events")]
        assert main.app.docs_url == "/api/docs" and main.app.openapi_url == "/api/openapi.json"
        # The auth fence moves with the routes, or every public path would start demanding a principal.
        assert "/api/slack/events" in main.PUBLIC_PATHS
        assert "/slack/events" not in main.PUBLIC_PATHS
        assert len(main.PUBLIC_PATHS) == len(main._BASE_PUBLIC_PATHS)


def test_prefixed_routes_are_reachable_and_the_auth_fence_still_bites(env):
    with api_at("/api") as main:
        client = TestClient(main.app)
        assert client.get("/health").status_code == 404
        r = client.get("/api/auth/me")
        assert r.status_code == 401 and r.headers.get("www-authenticate") == "Bearer"
        # The OpenAPI document is served from under the prefix too.
        assert client.get("/api/openapi.json").status_code == 200


# --- cookies -----------------------------------------------------------------------


def _fake_request(host: str = "testserver"):
    return SimpleNamespace(headers={"host": host}, url=SimpleNamespace(hostname=host))


def _set_cookie_path(header: str) -> str:
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k.lower() == "path":
            return v
    return ""


@pytest.mark.parametrize(("prefix", "expected"), [("", "/"), ("/api", "/api")])
def test_session_cookie_path_follows_the_prefix(env, prefix, expected):
    from api import deps

    env.setenv("STARTUPOS_PATH_PREFIX", prefix)
    resp = Response()
    deps.set_session_cookie(resp, _fake_request(), "cookie-value")
    assert _set_cookie_path(resp.headers["set-cookie"]) == expected

    cleared = Response()
    deps.clear_session_cookie(cleared, _fake_request())
    # Set and delete MUST agree: a delete on a different Path leaves the old cookie in the browser.
    assert _set_cookie_path(cleared.headers["set-cookie"]) == expected


@pytest.mark.parametrize(("prefix", "expected"), [("", "/auth/google"), ("/api", "/api/auth/google")])
def test_oauth_state_cookie_path_follows_the_prefix(env, prefix, expected, monkeypatch):
    """The one that kills sign-in silently: the state cookie is set on /auth/google and must be sent back to
    the callback under the SAME prefix, or every callback is a 'sign-in state mismatch'."""
    from auth import google

    env.setenv("GOOGLE_CLIENT_ID", "unit-test-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(
        google, "build_auth_url", lambda state, nonce: "https://accounts.google.com/o/oauth2/v2/auth?x=1"
    )

    with api_at(prefix) as main:
        client = TestClient(main.app, follow_redirects=False)
        r = client.get(config.prefixed("/auth/google"))
        assert r.status_code == 302
        header = next(h for h in r.headers.get_list("set-cookie") if h.startswith(google.OAUTH_COOKIE + "="))
        assert _set_cookie_path(header) == expected


# --- outbound URLs -----------------------------------------------------------------


def test_google_and_slack_urls_are_built_from_the_public_base(env):
    """STARTUPOS_PUBLIC_URL carries the prefix, so these need no prefix logic of their own — but they are what
    the operator pastes into Google and Slack, so pin them."""
    from auth import google, slack_install

    env.setenv("STARTUPOS_PUBLIC_URL", "http://localhost:8000")
    assert google.redirect_uri() == "http://localhost:8000/auth/google/callback"
    assert slack_install.redirect_uri() == "http://localhost:8000/slack/oauth/callback"

    env.setenv("STARTUPOS_PUBLIC_URL", "https://os.example.com/api")
    env.setenv("STARTUPOS_PATH_PREFIX", "/api")
    assert google.redirect_uri() == "https://os.example.com/api/auth/google/callback"
    assert slack_install.redirect_uri() == "https://os.example.com/api/slack/oauth/callback"


def test_public_url_prefix_mismatch_is_reported(env):
    env.setenv("STARTUPOS_PATH_PREFIX", "/api")
    env.setenv("STARTUPOS_PUBLIC_URL", "https://os.example.com")  # forgot the /api — Google will reject it
    warning = config.public_url_prefix_mismatch()
    assert warning and "STARTUPOS_PUBLIC_URL" in warning

    env.setenv("STARTUPOS_PUBLIC_URL", "https://os.example.com/api")
    assert config.public_url_prefix_mismatch() is None

    env.delenv("STARTUPOS_PATH_PREFIX")
    assert config.public_url_prefix_mismatch() is None
