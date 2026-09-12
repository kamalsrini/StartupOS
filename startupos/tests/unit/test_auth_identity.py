"""Principal resolution order: Bearer beats cookie; a bad bearer never falls back; inactive users are rejected."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from auth import identity, sessions, tokens


class _Conn:
    def __init__(self, tenant):
        self.tenant = tenant


@pytest.fixture()
def fake(monkeypatch):
    """Stub every DB touch. `state` describes what the lookups return."""
    state = {
        "token": ("unitone", "u_tok", "tok_1"),
        "session": {"tenant_id": "unitone", "user_id": "u_cookie"},
        "users": {
            "u_tok": {
                "id": "u_tok",
                "tenant_id": "unitone",
                "email": "t@x",
                "name": "T",
                "role": "owner",
                "status": "active",
            },
            "u_cookie": {
                "id": "u_cookie",
                "tenant_id": "unitone",
                "email": "c@x",
                "name": "C",
                "role": "owner",
                "status": "active",
            },
        },
        "touched": [],
        "conns": [],
    }

    @contextmanager
    def get_conn(dsn=None, tenant_id=None):
        state["conns"].append(tenant_id)
        yield _Conn(tenant_id)

    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", "unit-test-secret")
    monkeypatch.setattr(identity, "get_conn", get_conn)
    monkeypatch.setattr(
        tokens, "lookup_token", lambda conn, pt: state["token"] if tokens.looks_like_token(pt) else None
    )
    monkeypatch.setattr(tokens, "touch_token", lambda conn, tid: state["touched"].append(("token", tid)))
    monkeypatch.setattr(sessions, "load_session", lambda conn, sid: state["session"] if sid == "sid-1" else None)
    monkeypatch.setattr(sessions, "touch_session", lambda conn, sid: state["touched"].append(("session", sid)))

    def load_active_user(conn, user_id):
        u = state["users"].get(user_id)
        return None if not u or u["status"] != "active" else dict(u)

    monkeypatch.setattr(identity, "load_active_user", load_active_user)
    return state


GOOD_TOKEN = "sos_0123456789ab_" + "a" * 32


def test_bearer_wins_over_cookie(fake):
    cookie = sessions.sign_session_id("sid-1")
    p = identity.resolve(f"Bearer {GOOD_TOKEN}", cookie)
    assert p and p.via == "token" and p.user_id == "u_tok" and p.token_id == "tok_1"
    assert ("token", "tok_1") in fake["touched"]
    # lookup ran with NO tenant bound, user row read tenant-bound
    assert fake["conns"] == ["", "unitone"]


def test_cookie_alone(fake):
    cookie = sessions.sign_session_id("sid-1")
    p = identity.resolve(None, cookie)
    assert p and p.via == "session" and p.user_id == "u_cookie" and p.session_id == "sid-1"
    assert ("session", "sid-1") in fake["touched"]


def test_invalid_bearer_does_not_fall_back_to_cookie(fake):
    cookie = sessions.sign_session_id("sid-1")
    fake["token"] = None
    assert identity.resolve(f"Bearer {GOOD_TOKEN}", cookie) is None
    assert identity.resolve("Bearer garbage", cookie) is None
    assert identity.resolve("Basic abc", cookie) is not None  # not a bearer scheme → ignored, cookie used


def test_tampered_or_unknown_session(fake):
    assert identity.resolve(None, "sid-1") is None  # unsigned
    assert identity.resolve(None, sessions.sign_session_id("sid-2")) is None  # unknown
    assert identity.resolve(None, None) is None


def test_inactive_user_rejected(fake):
    fake["users"]["u_cookie"]["status"] = "disabled"
    assert identity.resolve(None, sessions.sign_session_id("sid-1")) is None
    fake["users"]["u_tok"]["status"] = "invited"
    assert identity.resolve(f"Bearer {GOOD_TOKEN}", None) is None


def test_principal_as_user_shape(fake):
    p = identity.resolve(f"Bearer {GOOD_TOKEN}", None)
    assert p.as_user() == {"id": "u_tok", "email": "t@x", "name": "T", "tenant_id": "unitone", "role": "owner"}
