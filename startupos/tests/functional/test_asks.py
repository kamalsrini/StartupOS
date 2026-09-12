"""Asks queue: API enqueues (no model), daemon services with a fake model, API returns the answer."""

import pytest

pytestmark = pytest.mark.functional


def test_ask_roundtrip_via_daemon(conn, monkeypatch, auth_env):
    import os

    from fastapi.testclient import TestClient

    from tests.conftest import login_as

    monkeypatch.setenv(
        "STARTUPOS_API_DSN", os.environ.get("STARTUPOS_TEST_DSN", "postgresql://postgres@localhost:5432/startupos_test")
    )
    from api.main import app

    client = TestClient(app)
    _, cookie = login_as(conn, "unitone", "asker@example.com")
    client.cookies.set("sos_session", cookie)
    r = client.post("/asks", json={"question": "what did we decide about pricing?", "mode": "answer"})
    assert r.status_code == 202 and r.json()["status"] == "pending"
    ask_id = r.json()["id"]

    # Daemon services the queue with a stubbed answer skill (no model in tests).
    from daemon import scheduler
    from daemon.skills.ask import answer as ask_answer

    monkeypatch.setattr(ask_answer, "answer", lambda ctx, q: f"stub answer to: {q}")
    from common.db import get_conn as _get_conn

    monkeypatch.setattr(
        scheduler, "get_conn", lambda **kw: _get_conn(os.environ["STARTUPOS_API_DSN"], tenant_id="unitone")
    )
    assert scheduler.service_asks("unitone") == 1

    r = client.get(f"/asks/{ask_id}")
    assert r.status_code == 200 and r.json()["status"] == "done"
    assert r.json()["answer"]["text"].startswith("stub answer to:")
    assert client.get("/asks/ask_nope").status_code == 404
