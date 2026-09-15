"""What a public deployment does NOT serve (Sprint 3d PE review, fix 2).

`/docs`, `/redoc` and `/openapi.json` are the product's own map: every route, every parameter, every schema,
handed to anyone who has the URL. They carry no tenant data, so this is reconnaissance rather than a breach —
but Sprint 3d put a real certificate on a URL meant to be shared, and the cost of switching them off is one
line, so they go off.

The switch keys off the variables a public deploy CANNOT work without (STARTUPOS_DOMAIN, STARTUPOS_PATH_PREFIX)
rather than a flag of its own, because a flag is something an operator can forget to set. STARTUPOS_ENABLE_DOCS
overrides in both directions for the cases where a human knows better.
"""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from auth import config

DOC_PATHS = ("/docs", "/redoc", "/openapi.json")
KEYS = ("STARTUPOS_PATH_PREFIX", "STARTUPOS_DOMAIN", "STARTUPOS_ENABLE_DOCS")


@contextmanager
def api_with(**env: str | None):
    """Reload api.main with these variables set (None deletes), then put the module back as it was."""
    import api.main

    before = {k: os.environ.get(k) for k in KEYS}
    for key in KEYS:
        os.environ.pop(key, None)
    for key, value in env.items():
        if value is not None:
            os.environ[key] = value
    try:
        yield importlib.reload(api.main)
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(api.main)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", "unit-test-session-secret-not-a-real-one")
    monkeypatch.setenv("STARTUPOS_API_DSN", "postgresql://unused@127.0.0.1:1/none")


# --- the switch itself ---------------------------------------------------------------


def test_a_bare_local_install_is_not_a_public_deployment(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    assert config.public_deployment() is False
    assert config.docs_enabled() is True


@pytest.mark.parametrize(
    ("key", "value"),
    [("STARTUPOS_DOMAIN", "os.example.com"), ("STARTUPOS_PATH_PREFIX", "/api")],
)
def test_either_half_of_the_public_topology_is_enough_to_close_the_docs(monkeypatch, key, value):
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(key, value)
    assert config.public_deployment() is True
    assert config.docs_enabled() is False


def test_an_operator_can_force_the_docs_either_way(monkeypatch):
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STARTUPOS_DOMAIN", "os.example.com")
    monkeypatch.setenv("STARTUPOS_ENABLE_DOCS", "1")
    assert config.docs_enabled() is True  # debugging a deploy, deliberately
    monkeypatch.setenv("STARTUPOS_ENABLE_DOCS", "0")
    assert config.docs_enabled() is False
    monkeypatch.delenv("STARTUPOS_DOMAIN")
    assert config.docs_enabled() is False  # …and off locally too, when asked


# --- what the app actually serves ----------------------------------------------------


def test_in_dev_the_docs_are_served():
    with api_with() as main:
        assert main.DOCS_ENABLED is True
        client = TestClient(main.app)
        for path in DOC_PATHS:
            assert client.get(path).status_code == 200, path
        assert set(DOC_PATHS) <= main.PUBLIC_PATHS
        assert main.app.openapi_url == "/openapi.json"
        assert client.get("/").json()["docs"] == "/docs"


def test_on_a_public_deployment_the_docs_routes_do_not_exist():
    with api_with(STARTUPOS_DOMAIN="os.example.com", STARTUPOS_PATH_PREFIX="/api") as main:
        assert main.DOCS_ENABLED is False
        client = TestClient(main.app)
        for path in DOC_PATHS:
            assert client.get(path).status_code == 404, path
            assert client.get("/api" + path).status_code == 404, path
        # 404 because they were never mounted — not a route answering 401, and not something to enumerate.
        assert main.app.docs_url is None and main.app.redoc_url is None and main.app.openapi_url is None
        assert not [p for p in main.PUBLIC_PATHS if p.endswith(DOC_PATHS)]
        # The OpenAPI document is where included-router paths are resolved (see test_path_prefix.py); the
        # document is still generated in memory, it is just not served.
        mounted = set(main.app.openapi()["paths"]) | {
            r.path for r in main.app.routes if getattr(r, "path", None) is not None
        }
        assert not [p for p in mounted if p.endswith(DOC_PATHS)]
        # The API root must not advertise a page that is not there.
        assert client.get("/api/").json()["docs"] is None
        # Everything else about the public deployment is unchanged: the routes are all still mounted under the
        # prefix (health needs a database, so it is the mount that is asserted here, not a 200) and the fence
        # still bites on anything that is not enumerated.
        assert {"/api/health", "/api/auth/google/callback"} <= mounted
        assert client.get("/api/auth/me").status_code == 401


def test_the_public_fence_stays_exactly_the_enumerated_set_in_both_modes():
    """The 'everything not in PUBLIC_PATHS answers 401' walker is only as true as PUBLIC_PATHS is accurate."""
    with api_with() as main:
        dev = main.PUBLIC_PATHS
    with api_with(STARTUPOS_DOMAIN="os.example.com") as main:
        public = main.PUBLIC_PATHS
    assert dev - public == set(DOC_PATHS) | {"/docs/oauth2-redirect"}
    assert public - dev == set()
    assert {"/health", "/auth/google/callback", "/onboarding/tenant", "/slack/events"} <= public
