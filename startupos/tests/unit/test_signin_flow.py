"""Track G — sign-in (CONTRACTS.md Sprint 3d).

The parts of the sign-in flow that do not need a database: the sign-up cookie that carries a verified Google
identity from the callback to `POST /onboarding/tenant`, the one Google failure that has a human answer
(an unverified address), and the copy the whole product decision rests on.

The database-bound half of the flow — a stranger reaching new-tenant onboarding, an existing user landing in
their OWN tenant, an inactive account refused, sign-out — is in tests/functional/test_auth_api.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth import config, google
from tests.unit.test_path_prefix import api_at

WEB = Path(__file__).resolve().parents[2] / "web"


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", "unit-test-session-secret")
    monkeypatch.setenv("STARTUPOS_API_DSN", "postgresql://unused@127.0.0.1:1/none")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "unit-test-client.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "unit-test-secret")
    monkeypatch.setenv("STARTUPOS_WEB_URL", "https://os.example.com")
    monkeypatch.setenv("STARTUPOS_PUBLIC_URL", "https://os.example.com")
    monkeypatch.delenv("STARTUPOS_PATH_PREFIX", raising=False)
    return monkeypatch


# --- the sign-up cookie ------------------------------------------------------------


def test_signup_cookie_round_trip(env):
    cookie = google.make_signup_cookie("header.payload.signature")
    assert google.read_signup_cookie(cookie) == "header.payload.signature"
    assert google.read_signup_cookie(cookie + "x") is None
    assert google.read_signup_cookie(None) is None
    assert google.read_signup_cookie("") is None
    assert google.read_signup_cookie("not-even-signed") is None


def test_signup_cookie_does_not_accept_an_oauth_cookie(env):
    """Separate salts: a state cookie can never be replayed as a sign-up, or vice versa."""
    assert google.read_signup_cookie(google.make_oauth_cookie("st", "nn")) is None
    assert google.read_oauth_cookie(google.make_signup_cookie("x.y.z")) is None


def test_signup_cookie_expires(env, monkeypatch):
    cookie = google.make_signup_cookie("x.y.z")
    monkeypatch.setattr(google, "SIGNUP_COOKIE_MAX_AGE", -1)
    assert google.read_signup_cookie(cookie) is None


def test_unverified_email_is_its_own_error():
    """The routers answer this one differently, so it has to be catchable on its own."""
    assert issubclass(google.UnverifiedEmailError, google.GoogleAuthError)


# --- the callback, without a database ------------------------------------------------


class _NoSuchUser:
    """A connection whose auth_lookup_google finds nobody — the stranger case."""

    def execute(self, *_a, **_k):
        return self

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


@pytest.mark.parametrize(("prefix", "expected"), [("", "/onboarding"), ("/api", "/api/onboarding")])
def test_new_identity_gets_a_signup_cookie_the_onboarding_route_will_receive(env, monkeypatch, prefix, expected):
    """The cookie has to be sent back to POST /onboarding/tenant — under the prefix, that is /api/onboarding.

    Same trap as the OAuth state cookie: a Path the browser will not match is a sign-up that silently dies.
    """
    import api.routers.auth as auth_router

    monkeypatch.setattr(auth_router, "get_conn", lambda *_a, **_k: _NoSuchUser())
    monkeypatch.setattr(google, "exchange_code", lambda code, client=None: {"id_token": "id.tok.en"})
    monkeypatch.setattr(
        google,
        "verify_id_token",
        lambda tok, nonce=None, client=None, audience=None: {
            "sub": "g-1",
            "email": "Stranger@Example.com",
            "email_verified": True,
            "name": "A Stranger",
        },
    )
    env.setenv("STARTUPOS_PATH_PREFIX", prefix)
    if prefix:
        env.setenv("STARTUPOS_PUBLIC_URL", "https://os.example.com" + prefix)

    started: dict[str, str] = {}

    def fake_auth_url(state, nonce, client=None):
        started["state"] = state
        return "https://accounts.google.com/o/oauth2/v2/auth?x=1"

    monkeypatch.setattr(google, "build_auth_url", fake_auth_url)

    with api_at(prefix) as main:
        client = TestClient(main.app, follow_redirects=False)
        # Start for real, so the state cookie is the one the API itself wrote, at the Path it chose.
        assert client.get(config.prefixed("/auth/google")).status_code == 302
        r = client.get(config.prefixed("/auth/google/callback"), params={"code": "c0de", "state": started["state"]})

    assert r.status_code == 302, r.text
    # The address is lower-cased and handed to the page so it can name it in the "this is a NEW company" copy.
    assert r.headers["location"] == "https://os.example.com/login?new=google&email=stranger%40example.com"
    header = next(h for h in r.headers.get_list("set-cookie") if h.startswith(google.SIGNUP_COOKIE + "="))
    assert f"Path={expected}" in header
    assert "HttpOnly" in header  # the id_token must never be readable from JavaScript
    assert "id.tok.en" not in r.headers["location"]  # nor from the URL bar, history or a proxy log
    assert google.read_signup_cookie(r.cookies[google.SIGNUP_COOKIE]) == "id.tok.en"


def test_a_state_mismatch_is_a_page_a_person_can_act_on(env, monkeypatch):
    with api_at("") as main:
        client = TestClient(main.app, follow_redirects=False)
        r = client.get("/auth/google/callback", params={"code": "c", "state": "forged"})
    assert r.status_code == 403
    assert "start again" in r.text.lower() and "https://os.example.com/login" in r.text


# --- the copy the product decision rests on ------------------------------------------


def test_the_sign_up_page_says_it_is_a_new_company_and_how_to_join_an_existing_one():
    """The bad outcome Sprint 3d names by hand: someone invited to a colleague's company lands in a brand-new
    empty tenant and thinks StartupOS lost their data. The only defence is copy, so the copy is a gate."""
    page = (WEB / "app" / "login" / "page.tsx").read_text()
    assert "a new company of your own" in page
    assert "brand-new, empty workspace" in page
    assert "invite-only" in page
    assert "Were you invited to a colleague" in page  # …tells them exactly what to do instead
    assert "not</b> how you join" in page


def test_the_sign_in_page_says_so_when_the_operator_has_closed_sign_up():
    """STARTUPOS_ALLOW_SIGNUP=0 (Sprint 3d PE review). The API refuses, but a refusal at the END of a Google
    round trip is the dead end Track G existed to remove, so the page has to say it up front — and must not
    show the "create my company" form to a stale `?new=google` link either. Asserted on the source, like the
    copy gate above, because there is no JS test runner here."""
    page = (WEB / "app" / "login" / "page.tsx").read_text()
    assert "avail.signup ? signUpForParam : null" in page  # no sign-up form when the door is shut
    assert "{avail.signup ? (" in page
    assert "New companies are closed on this installation." in page
    assert "signup_closed:" in page  # and the ?error= the callback sends back has copy of its own

    api = (WEB / "lib" / "api.ts").read_text()
    assert "signup: boolean" in api  # GET /auth/providers carries it


def test_the_front_door_sends_a_signed_out_visitor_to_login():
    root = (WEB / "app" / "page.tsx").read_text()
    assert '"/login"' in root and '"/cockpit"' in root
    config_js = (WEB / "next.config.mjs").read_text()
    assert "destination" not in config_js  # no redirect can outrank the check above


# --- open redirect on the one hostname the founder tells people to trust ---------------


def test_the_login_page_never_sends_the_browser_to_a_raw_next_parameter():
    """`/login?next=…` is a link anyone can build, and the page follows it the moment it sees a live session.

    Unfiltered, that is an open redirect on the domain the whole product's trust rests on — the link really is
    os.example.com, and it lands on the attacker's page. There is no JS test runner here, so this asserts on the
    source the same way the copy gate above does.
    """
    page = (WEB / "app" / "login" / "page.tsx").read_text()
    assert 'safeNext(params.get("next"))' in page
    assert 'params.get("next") ||' not in page  # the unfiltered form must not come back

    api = (WEB / "lib" / "api.ts").read_text()
    assert "export function safeNext(" in api
    body = api.split("export function safeNext(", 1)[1].split("\n}", 1)[0]
    assert 'startsWith("/")' in body  # same-origin paths only …
    assert 'startsWith("//")' in body and 'startsWith("/\\\\")' in body  # … and not protocol-relative ones
