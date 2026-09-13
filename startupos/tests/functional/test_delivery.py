"""Track D end-to-end: two tenants, two Slack workspaces, one daemon.

Everything here runs on RLS-bound `startupos_app` connections (one per tenant), because the point of Sprint 3b
is that tenant A's pulse goes to tenant A's channel with tenant A's bot token and lands in tenant A's ledger —
and that a failure in one delivery never stops the tick. No network: the Slack write path is a recorder.
"""

from __future__ import annotations

import base64
import os
from contextlib import contextmanager

import pytest

from common import secrets
from daemon import delivery, llm, scheduler
from daemon.executors import slack as slack_executor
from daemon.skills import build_ctx
from daemon.skills.cockpit import evening_digest, morning_pulse
from tests.conftest import app_conn_for

pytestmark = pytest.mark.functional

ACME, GLOBEX, WEBONLY = "dlv-acme", "dlv-globex", "dlv-webonly"
SETUP = {
    ACME: {"channel": "#acme-ops", "token": "xoxb-acme", "mode": "slack"},
    GLOBEX: {"channel": "C0GLOBEX", "token": "xoxb-globex", "mode": "both"},
    WEBONLY: {"channel": "#never", "token": "xoxb-webonly", "mode": "web"},
}


class Recorder:
    """Stands in for the Slack executor and records (channel, token, blocks) per post."""

    def __init__(self, fail_on: str | None = None):
        self.calls: list[dict] = []
        self.fail_on = fail_on

    def __call__(self, inp, *, client=None, token=None):
        self.calls.append({"channel": inp["channel"], "token": token, "text": inp["text"], "blocks": inp["blocks"]})
        if self.fail_on and self.fail_on in inp["channel"]:
            raise slack_executor.SlackError("ratelimited")
        return {"text": "Posted", "url": "u", "ts": f"17254000{len(self.calls):02d}.0001", "channel": inp["channel"]}

    def for_channel(self, channel: str) -> list[dict]:
        return [c for c in self.calls if c["channel"] == channel]


@pytest.fixture()
def tenants(conn, monkeypatch):
    """Three tenants with their own Slack channel and their own bot token, plus a master key and no model."""
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_client_factory", llm._default_client_factory)
    for tid, cfg in SETUP.items():
        conn.execute(
            """INSERT INTO tenants (id, name, pulse_channel, slack_channel) VALUES (%s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET pulse_channel = EXCLUDED.pulse_channel,
                                              slack_channel = EXCLUDED.slack_channel""",
            (tid, tid, cfg["mode"], cfg["channel"]),
        )
        for sql in (
            "DELETE FROM deliveries WHERE tenant_id = %s",
            "DELETE FROM approvals WHERE tenant_id = %s",
            "DELETE FROM runs WHERE tenant_id = %s",
            "DELETE FROM signals WHERE tenant_id = %s",
            "DELETE FROM connections WHERE tenant_id = %s",
            "DELETE FROM tenant_secrets WHERE tenant_id = %s",
        ):
            conn.execute(sql, (tid,))
        secrets.put(conn, tid, "slack_bot_token", cfg["token"])
        conn.execute(
            """INSERT INTO connections (id, tenant_id, source, secret_ref)
               VALUES (%s, %s, 'slack', 'kv:slack_bot_token')
               ON CONFLICT (tenant_id, source) DO UPDATE SET secret_ref = EXCLUDED.secret_ref""",
            (f"{tid}-slack", tid),
        )
    conn.commit()
    yield SETUP
    for tid in SETUP:
        for sql in (
            "DELETE FROM deliveries WHERE tenant_id = %s",
            "DELETE FROM approvals WHERE tenant_id = %s",
            "DELETE FROM runs WHERE tenant_id = %s",
            "DELETE FROM signals WHERE tenant_id = %s",
            "DELETE FROM connections WHERE tenant_id = %s",
            "DELETE FROM tenant_secrets WHERE tenant_id = %s",
        ):
            conn.execute(sql, (tid,))
    conn.commit()


@pytest.fixture()
def recorder(monkeypatch):
    r = Recorder()
    monkeypatch.setattr(delivery.slack_executor, "post_message", r)
    return r


def seed_high_signal(c, tenant_id, sid, title, rule_id="build.unassigned_high"):
    c.execute(
        """INSERT INTO signals (id, tenant_id, module, rule_id, severity, kind, title)
           VALUES (%s, %s, 'build', %s, 'high', 'click', %s)
           ON CONFLICT (tenant_id, id) DO NOTHING""",
        (sid, tenant_id, rule_id, title),
    )


def ledger(c, tenant_id):
    return [
        (r["kind"], r["ref"], r["channel"], r["status"])
        for r in c.execute(
            "SELECT kind, ref, channel, status FROM deliveries WHERE tenant_id = %s ORDER BY id", (tenant_id,)
        ).fetchall()
    ]


# --- two tenants, two workspaces --------------------------------------------------------------------


def test_two_tenants_deliver_to_their_own_channel_with_their_own_token(tenants, recorder):
    for tid in (ACME, GLOBEX):
        with app_conn_for(tid) as c:
            out = delivery.deliver(c, tid, "pulse", {"ref": "pulse:2026-09-13", "text": f"pulse for {tid}"})
            assert out["status"] == "sent"
    assert [(c["channel"], c["token"]) for c in recorder.calls] == [
        ("#acme-ops", "xoxb-acme"),
        ("C0GLOBEX", "xoxb-globex"),
    ]
    assert "pulse for dlv-acme" in recorder.calls[0]["blocks"][1]["text"]["text"]
    assert "pulse for dlv-globex" in recorder.calls[1]["blocks"][1]["text"]["text"]


def test_the_ledger_is_tenant_isolated_under_rls(tenants, recorder):
    for tid in (ACME, GLOBEX):
        with app_conn_for(tid) as c:
            delivery.deliver(c, tid, "pulse", {"ref": "pulse:2026-09-13", "text": "x"})
    with app_conn_for(ACME) as c:
        assert ledger(c, ACME) == [("pulse", "pulse:2026-09-13", "#acme-ops", "sent")]
        assert c.execute("SELECT count(*) AS n FROM deliveries").fetchone()["n"] == 1  # only its own rows exist
        assert ledger(c, GLOBEX) == []


def test_dedupe_holds_across_connections_and_transactions(tenants, recorder):
    payload = {"ref": "digest:2026-09-13", "text": "evening"}
    with app_conn_for(ACME) as c:
        assert delivery.deliver(c, ACME, "digest", payload)["status"] == "sent"
    with app_conn_for(ACME) as c:  # a second daemon pass, a fresh connection
        assert delivery.deliver(c, ACME, "digest", payload)["reason"] == "already delivered"
    assert len(recorder.calls) == 1


# --- the cockpit skills deliver and still return their text ------------------------------------------


def test_morning_pulse_delivers_and_still_returns_the_web_text(tenants, recorder):
    with app_conn_for(GLOBEX) as c:  # pulse_channel = 'both'
        text = morning_pulse.run(build_ctx(c, GLOBEX))
        assert text.startswith("Morning pulse (Tier 0)")  # no model configured → the Tier-0 fallback
        assert ledger(c, GLOBEX) == [("pulse", f"pulse:{_today()}", "C0GLOBEX", "sent")]
    posted = recorder.for_channel("C0GLOBEX")
    assert len(posted) == 1 and posted[0]["token"] == "xoxb-globex"
    assert posted[0]["text"].startswith("Morning pulse (Tier 0)")  # notification fallback, never empty
    assert posted[0]["blocks"][0]["type"] == "header"


def test_evening_digest_delivers_and_still_returns_the_web_text(tenants, recorder):
    with app_conn_for(ACME) as c:
        text = evening_digest.run(build_ctx(c, ACME))
        assert "Evening digest" in text
        assert ledger(c, ACME) == [("digest", f"digest:{_today()}", "#acme-ops", "sent")]
    assert recorder.for_channel("#acme-ops")[0]["token"] == "xoxb-acme"


def test_a_web_only_tenant_posts_nothing_but_still_gets_its_pulse(tenants, recorder):
    with app_conn_for(WEBONLY) as c:
        text = morning_pulse.run(build_ctx(c, WEBONLY))
        assert text.startswith("Morning pulse (Tier 0)")
        assert ledger(c, WEBONLY) == [("pulse", f"pulse:{_today()}", None, "skipped")]
    assert recorder.calls == []


def test_a_failing_delivery_never_fails_the_skill(tenants, monkeypatch):
    monkeypatch.setattr(delivery.slack_executor, "post_message", Recorder(fail_on="#acme-ops"))
    with app_conn_for(ACME) as c:
        text = morning_pulse.run(build_ctx(c, ACME))
        assert text.startswith("Morning pulse (Tier 0)")  # the founder still has the pulse
        assert ledger(c, ACME) == [("pulse", f"pulse:{_today()}", "#acme-ops", "failed")]
        err = c.execute("SELECT error FROM deliveries WHERE tenant_id = %s", (ACME,)).fetchone()["error"]
        assert err.startswith("SlackError: ratelimited")


# --- the 15-minute tick ------------------------------------------------------------------------------


@contextmanager
def _tenant_conn(*_a, tenant_id=None, **_k):
    with app_conn_for(tenant_id) as c:
        yield c


def test_the_tick_delivers_new_high_signals_once_per_signal(tenants, recorder, monkeypatch):
    monkeypatch.setattr(scheduler, "get_conn", _tenant_conn)
    with app_conn_for(ACME) as c:
        for i in range(7):
            seed_high_signal(c, ACME, f"s{i}", f"UNI-{i} is High and unassigned")
    out = scheduler.deliver_signals(ACME)
    assert [o["status"] for o in out] == ["sent"] * 5
    assert len(recorder.calls) == 5
    assert "+2 more waiting in the cockpit" in recorder.calls[-1]["blocks"][-1]["elements"][0]["text"]
    # a second tick delivers the rest and then nothing
    assert len(scheduler.deliver_signals(ACME)) == 2
    assert scheduler.deliver_signals(ACME) == []
    assert len(recorder.calls) == 7
    with app_conn_for(ACME) as c:
        assert {r[3] for r in ledger(c, ACME)} == {"sent"}


def test_one_tenants_slack_outage_does_not_stop_the_other_tenant(tenants, monkeypatch):
    rec = Recorder(fail_on="#acme-ops")
    monkeypatch.setattr(delivery.slack_executor, "post_message", rec)
    monkeypatch.setattr(scheduler, "get_conn", _tenant_conn)
    for tid in (ACME, GLOBEX):
        with app_conn_for(tid) as c:
            seed_high_signal(c, tid, "s1", "UNI-1 is High and unassigned")
    assert [o["status"] for o in scheduler.deliver_signals(ACME)] == ["failed"]
    assert [o["status"] for o in scheduler.deliver_signals(GLOBEX)] == ["sent"]
    with app_conn_for(ACME) as c:
        assert ledger(c, ACME) == [("signal", "signal:s1", "#acme-ops", "failed")]
    with app_conn_for(GLOBEX) as c:
        assert ledger(c, GLOBEX) == [("signal", "signal:s1", "C0GLOBEX", "sent")]
    # the failed one is retried on the next tick, and lands once Slack recovers
    monkeypatch.setattr(delivery.slack_executor, "post_message", Recorder())
    assert [o["status"] for o in scheduler.deliver_signals(ACME)] == ["sent"]


def test_tick_15m_delivers_and_still_executes_approvals_when_slack_is_down(tenants, monkeypatch):
    """A Slack outage must not cost the founder an executed approval: delivery is best effort inside the tick."""
    monkeypatch.setattr(delivery.slack_executor, "post_message", Recorder(fail_on="#acme-ops"))
    monkeypatch.setattr(scheduler, "get_conn", _tenant_conn)
    with app_conn_for(ACME) as c:
        # a Chief-of-Staff risk: the signal engine that runs first in the tick owns only its own rule ids
        seed_high_signal(c, ACME, "cos.risk:cash", "Runway dips below 6 months", rule_id="cos.risk")
        c.execute(
            """INSERT INTO approvals (id, tenant_id, module, type, target, preview, status, decided_at)
               VALUES ('rec-1', %s, 'build', 'Brex · pay', 'B1', 'record only', 'approved', now())""",
            (ACME,),
        )
    scheduler.tick_15m(ACME)  # must not raise
    with app_conn_for(ACME) as c:
        assert ledger(c, ACME) == [("signal", "signal:cos.risk:cash", "#acme-ops", "failed")]
        row = c.execute("SELECT status FROM approvals WHERE tenant_id = %s AND id = 'rec-1'", (ACME,)).fetchone()
        assert row["status"] == "executed"  # the executors ran after the failed delivery


def _today() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).date().isoformat()
