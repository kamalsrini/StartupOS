"""Track D — daemon/delivery.py: channel resolution, the tenant's own token, the deliveries ledger, dedupe,
and the failure paths that must never reach the caller (no token, bad channel, Slack rate limit, oversized text).

Uses the scratch Postgres (the `conn` fixture) because the ledger and the dedupe ARE the unique constraint.
No network: the Slack write path is either a recorded fake or the real executor with a fake client.
"""

from __future__ import annotations

import base64
import os

import pytest
from track_b_fakes import FakeSlackClient

from common import secrets
from daemon import delivery
from daemon import slack_blocks as sb
from daemon.executors import slack as slack_executor

TA = "del-a"
TB = "del-b"


class Poster:
    """Stands in for daemon.executors.slack.post_message and records what delivery handed it."""

    def __init__(self, *, fail: Exception | None = None):
        self.calls: list[dict] = []
        self.fail = fail
        self.n = 0

    def __call__(self, inp, *, client=None, token=None):
        self.n += 1
        self.calls.append({"inp": inp, "token": token})
        if self.fail:
            raise self.fail
        return {
            "text": "Posted",
            "url": "https://slack/x",
            "ts": f"172540000{self.n}.000100",
            "channel": inp["channel"],
        }

    @property
    def channels(self) -> list[str]:
        return [c["inp"]["channel"] for c in self.calls]

    @property
    def tokens(self) -> list[str | None]:
        return [c["token"] for c in self.calls]


@pytest.fixture()
def master(monkeypatch):
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())


@pytest.fixture()
def poster(monkeypatch):
    p = Poster()
    monkeypatch.setattr(delivery.slack_executor, "post_message", p)
    return p


def setup_tenant(conn, tenant_id, *, mode="slack", channel="#ops", token="xoxb-token"):
    conn.execute(
        """INSERT INTO tenants (id, name, pulse_channel, slack_channel) VALUES (%s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET pulse_channel = EXCLUDED.pulse_channel,
                                          slack_channel = EXCLUDED.slack_channel""",
        (tenant_id, tenant_id, mode, channel or ""),
    )
    conn.execute("DELETE FROM deliveries WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM signals WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM slack_installations WHERE tenant_id = %s", (tenant_id,))
    conn.execute("DELETE FROM connections WHERE tenant_id = %s AND source = 'slack'", (tenant_id,))
    conn.execute("DELETE FROM tenant_secrets WHERE tenant_id = %s", (tenant_id,))
    if token:
        secrets.put(conn, tenant_id, "slack_bot_token", token)
        conn.execute(
            """INSERT INTO connections (id, tenant_id, source, secret_ref) VALUES (%s, %s, 'slack', 'kv:slack_bot_token')
               ON CONFLICT (tenant_id, source) DO UPDATE SET secret_ref = EXCLUDED.secret_ref""",
            (f"{tenant_id}-slack", tenant_id),
        )
    return tenant_id


def rows(conn, tenant_id):
    return [
        dict(r)
        for r in conn.execute(
            "SELECT kind, ref, channel, ts, status, error FROM deliveries WHERE tenant_id = %s ORDER BY id",
            (tenant_id,),
        ).fetchall()
    ]


def seed_signal(conn, tenant_id, sid, *, severity="high", rule_id="build.unassigned_high", title="t", minutes=0):
    conn.execute(
        """INSERT INTO signals (id, tenant_id, module, rule_id, severity, kind, title, first_seen_at)
           VALUES (%s, %s, 'build', %s, %s, 'click', %s, now() - make_interval(mins => %s))
           ON CONFLICT (tenant_id, id) DO UPDATE SET severity = EXCLUDED.severity, resolved_at = NULL""",
        (sid, tenant_id, rule_id, severity, title, minutes),
    )


PULSE = {"ref": "pulse:2026-09-13", "text": "four lines", "approvals_count": 2, "tiles": []}


# --- channel resolution ---------------------------------------------------------------------------


def test_web_only_tenant_never_posts_and_is_recorded_as_skipped(conn, master, poster):
    setup_tenant(conn, TA, mode="web")
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "skipped" and "web" in out["reason"]
    assert poster.calls == []
    assert rows(conn, TA) == [
        {
            "kind": "pulse",
            "ref": "pulse:2026-09-13",
            "channel": None,
            "ts": None,
            "status": "skipped",
            "error": out["reason"],
        }
    ]


def test_web_is_honoured_even_when_a_channel_is_passed_explicitly(conn, master, poster):
    setup_tenant(conn, TA, mode="web")
    out = delivery.deliver(conn, TA, "pulse", PULSE, channel="#anywhere")
    assert out["status"] == "skipped" and poster.calls == []


def test_both_posts_to_the_tenant_channel(conn, master, poster):
    setup_tenant(conn, TA, mode="both", channel="#founders")
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "sent" and poster.channels == ["#founders"]


def test_explicit_channel_wins_over_the_tenant_default(conn, master, poster):
    setup_tenant(conn, TA, mode="slack", channel="#ops")
    delivery.deliver(conn, TA, "pulse", PULSE, channel="C0ALERTS")
    assert poster.channels == ["C0ALERTS"]


def test_no_channel_configured_skips_cleanly(conn, master, poster):
    setup_tenant(conn, TA, mode="slack", channel=None)
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "skipped" and out["reason"] == "no Slack channel configured"
    assert poster.calls == []


def test_resolve_channel_reads_a_direct_channel_name_in_pulse_channel(conn, master):
    setup_tenant(conn, TA, mode="slack", channel=None)
    conn.execute("UPDATE tenants SET pulse_channel = '#legacy' WHERE id = %s", (TA,))
    assert delivery.resolve_channel(conn, TA)["channel"] == "#legacy"


def test_install_channel_is_none_without_the_install_table(conn, master):
    """Track I owns slack_installations; delivery degrades to "no channel" when it is absent."""
    assert delivery.install_channel(conn, TA) is None


def test_unknown_tenant_resolves_to_no_channel(conn, master):
    """A tenant row that does not exist (or an RLS-invisible one) resolves to "do not post", never to a default."""
    assert delivery.resolve_channel(conn, "no-such-tenant")["channel"] is None
    assert delivery.tenant_delivery_settings(conn, "no-such-tenant") == {"mode": "web", "channel": None}


def install_row(conn, tenant_id, channel):
    conn.execute(
        """INSERT INTO slack_installations (tenant_id, team_id, team_name, bot_user_id, default_channel)
           VALUES (%s, %s, %s, 'U0BOT', %s)
           ON CONFLICT (tenant_id) DO UPDATE SET default_channel = EXCLUDED.default_channel, revoked_at = NULL""",
        (tenant_id, f"T-{tenant_id}", tenant_id, channel),
    )


def test_an_unset_tenant_channel_falls_back_to_the_install_channel(conn, master, poster):
    """`tenants.slack_channel` unset means "post where the founder put StartupOS while installing Slack"."""
    setup_tenant(conn, TA, mode="slack", channel=None)
    install_row(conn, TA, "#founders")
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "sent" and poster.channels == ["#founders"]


def test_the_tenant_channel_overrides_the_install_channel(conn, master, poster):
    setup_tenant(conn, TA, mode="slack", channel="#ops")
    install_row(conn, TA, "#founders")
    assert delivery.deliver(conn, TA, "pulse", PULSE)["status"] == "sent"
    assert poster.channels == ["#ops"]


# --- token ---------------------------------------------------------------------------------------


def test_tenant_without_a_slack_credential_skips_and_never_raises(conn, master, poster):
    setup_tenant(conn, TA, token=None)
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "skipped" and out["reason"] == "no Slack credential for this tenant"
    assert poster.calls == []
    # PE follow-up: no ledger row at all — "Slack is not connected yet" must not consume the ref forever
    assert rows(conn, TA) == []


def test_a_broken_master_key_is_a_skip_not_a_crash(conn, monkeypatch, master, poster):
    setup_tenant(conn, TA)
    monkeypatch.setenv("STARTUPOS_MASTER_KEY", "not-base64!!")
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "skipped" and poster.calls == []


def test_each_tenant_posts_with_its_own_token_and_channel(conn, master, poster):
    setup_tenant(conn, TA, channel="#a", token="xoxb-a")
    setup_tenant(conn, TB, channel="#b", token="xoxb-b")
    delivery.deliver(conn, TA, "pulse", PULSE)
    delivery.deliver(conn, TB, "pulse", PULSE)
    assert poster.channels == ["#a", "#b"]
    assert poster.tokens == ["xoxb-a", "xoxb-b"]


# --- the ledger and dedupe ------------------------------------------------------------------------


def test_a_sent_delivery_records_channel_and_ts(conn, master, poster):
    setup_tenant(conn, TA, channel="#ops")
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "sent" and out["ts"]
    row = rows(conn, TA)[0]
    assert row == {
        "kind": "pulse",
        "ref": "pulse:2026-09-13",
        "channel": "#ops",
        "ts": out["ts"],
        "status": "sent",
        "error": None,
    }


def test_dedupe_blocks_a_second_send_of_the_same_ref(conn, master, poster):
    setup_tenant(conn, TA)
    first = delivery.deliver(conn, TA, "pulse", PULSE)
    second = delivery.deliver(conn, TA, "pulse", PULSE)
    assert first["status"] == "sent"
    assert second["status"] == "skipped" and second["reason"] == "already delivered"
    assert poster.n == 1
    assert len(rows(conn, TA)) == 1


def test_dedupe_is_per_tenant(conn, master, poster):
    setup_tenant(conn, TA)
    setup_tenant(conn, TB)
    assert delivery.deliver(conn, TA, "pulse", PULSE)["status"] == "sent"
    assert delivery.deliver(conn, TB, "pulse", PULSE)["status"] == "sent"
    assert poster.n == 2


def test_a_different_day_is_a_different_ref(conn, master, poster):
    setup_tenant(conn, TA)
    delivery.deliver(conn, TA, "pulse", {**PULSE, "ref": "pulse:2026-09-13"})
    delivery.deliver(conn, TA, "pulse", {**PULSE, "ref": "pulse:2026-09-14"})
    assert poster.n == 2


def test_a_failed_delivery_is_retried_on_the_next_pass(conn, master, monkeypatch):
    setup_tenant(conn, TA)
    boom = Poster(fail=slack_executor.SlackError("ratelimited"))
    monkeypatch.setattr(delivery.slack_executor, "post_message", boom)
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "failed" and "ratelimited" in out["error"]
    good = Poster()
    monkeypatch.setattr(delivery.slack_executor, "post_message", good)
    assert delivery.deliver(conn, TA, "pulse", PULSE)["status"] == "sent"
    assert len(rows(conn, TA)) == 1 and rows(conn, TA)[0]["status"] == "sent"


# --- failure paths --------------------------------------------------------------------------------


def test_a_slack_rate_limit_records_failed_and_does_not_raise(conn, master, monkeypatch):
    setup_tenant(conn, TA)
    monkeypatch.setattr(delivery.slack_executor, "post_message", Poster(fail=slack_executor.SlackError("ratelimited")))
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "failed"
    row = rows(conn, TA)[0]
    assert row["status"] == "failed" and row["error"].startswith("SlackError: ratelimited")


def test_a_bad_channel_records_the_slack_error(conn, master, monkeypatch):
    setup_tenant(conn, TA, channel="#does-not-exist")
    monkeypatch.setattr(
        delivery.slack_executor, "post_message", Poster(fail=slack_executor.SlackError("channel_not_found"))
    )
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "failed" and "channel_not_found" in out["error"]
    assert rows(conn, TA)[0]["channel"] == "#does-not-exist"


def test_an_unexpected_exception_is_also_contained(conn, master, monkeypatch):
    setup_tenant(conn, TA)
    monkeypatch.setattr(delivery.slack_executor, "post_message", Poster(fail=ZeroDivisionError("nope")))
    assert delivery.deliver(conn, TA, "pulse", PULSE)["status"] == "failed"


def test_blocks_slack_would_reject_are_never_sent(conn, master, poster, monkeypatch):
    setup_tenant(conn, TA)
    monkeypatch.setattr(delivery.slack_blocks, "validate", lambda blocks: ["block 0: 9000 chars > 3000"])
    out = delivery.deliver(conn, TA, "pulse", PULSE)
    assert out["status"] == "failed" and "invalid Block Kit" in out["error"]
    assert poster.calls == []


def test_a_7000_character_pulse_is_delivered_inside_the_limits(conn, master, poster):
    setup_tenant(conn, TA)
    out = delivery.deliver(conn, TA, "pulse", {**PULSE, "text": "líne ✅\n" * 1200})
    assert out["status"] == "sent"
    blocks = poster.calls[0]["inp"]["blocks"]
    assert sb.validate(blocks) == []
    assert all(len(b.get("text", {}).get("text", "")) <= 3000 for b in blocks)


def test_unicode_reaches_the_executor_unchanged(conn, master, poster):
    setup_tenant(conn, TA)
    delivery.deliver(conn, TA, "pulse", {**PULSE, "text": "🚀 明日 · café"})
    assert "🚀 明日 · café" in poster.calls[0]["inp"]["blocks"][1]["text"]["text"]
    assert poster.calls[0]["inp"]["text"] == "🚀 明日 · café"


def test_an_unknown_kind_is_a_programming_error(conn, master):
    setup_tenant(conn, TA)
    with pytest.raises(delivery.UnknownKind):
        delivery.deliver(conn, TA, "telegram", {})


def test_deliver_approval_is_implemented_by_track_b(conn, master, poster):
    """Track D's extension point is filled in by Track B; the full behaviour lives in tests/*_slack_actions.py."""
    setup_tenant(conn, TA)
    out = delivery.deliver_approval(conn, TA, {"id": "a1", "type": "issue", "target": "UNI-1", "preview": "p"})
    assert out["status"] == "sent"
    assert out["ref"] == "approval:a1"
    assert poster.calls[0]["inp"]["blocks"][-2]["type"] == "actions"


# --- refs, payloads and the executor contract -----------------------------------------------------


def test_default_ref_shapes():
    from datetime import UTC, datetime

    day = datetime(2026, 9, 13, 7, 0, tzinfo=UTC)
    assert delivery.default_ref("pulse", {"date": day}) == "pulse:2026-09-13"
    assert delivery.default_ref("digest", {"date": day}) == "digest:2026-09-13"
    assert delivery.default_ref("signal", {"signal": {"id": "s:1"}}) == "signal:s:1"
    assert delivery.default_ref("approval", {"approval": {"id": "a1"}}) == "approval:a1"


def test_pulse_and_digest_payloads_carry_tier0_counts(conn, master):
    setup_tenant(conn, TA)
    seed_signal(conn, TA, "s1", severity="high")
    seed_signal(conn, TA, "s2", severity="low")
    conn.execute(
        """INSERT INTO approvals (id, tenant_id, module, type, target, preview, status)
           VALUES ('ap1', %s, 'build', 't', 'UNI-1', 'p', 'pending') ON CONFLICT (tenant_id, id) DO NOTHING""",
        (TA,),
    )
    p = delivery.pulse_payload(conn, TA, "text")
    assert p["approvals_count"] == 1
    assert [t["val"] for t in p["tiles"]] == ["1", "2", "1"]
    d = delivery.digest_payload(conn, TA, "text")
    assert d["approvals_pending"] == 1 and d["open_signals"] == 2
    assert p["ref"].startswith("pulse:") and d["ref"].startswith("digest:")


def test_the_executor_passes_blocks_through_to_chat_postmessage():
    """The executor stays the only Slack write path; `blocks` is optional and `text` stays the fallback."""
    client = FakeSlackClient()
    _, blocks = sb.signal_blocks({"id": "s1", "title": "t", "severity": "high", "module": "build"})
    out = slack_executor.post_message({"channel": "C1", "text": "fallback", "blocks": blocks}, client=client)
    assert client.posted[0]["blocks"] == blocks and client.posted[0]["text"] == "fallback"
    assert out["ts"] == "1725400000.000100" and out["channel"] == "C1"
    # unchanged for callers that pass no blocks
    slack_executor.post_message({"channel": "C1", "text": "plain"}, client=client)
    assert "blocks" not in client.posted[1]


# --- signal fan-out -------------------------------------------------------------------------------


def test_only_high_and_chief_of_staff_signals_are_delivered(conn, master, poster):
    setup_tenant(conn, TA)
    seed_signal(conn, TA, "hi", severity="high", minutes=30)
    seed_signal(conn, TA, "lo", severity="low", minutes=20)
    seed_signal(conn, TA, "cos.risk:cash", severity="medium", rule_id="cos.risk", minutes=10)
    out = delivery.deliver_signals(conn, TA)
    assert [o["ref"] for o in out] == ["signal:hi", "signal:cos.risk:cash"]
    assert all(o["status"] == "sent" for o in out)
    assert poster.n == 2


def test_resolved_signals_are_not_delivered(conn, master, poster):
    setup_tenant(conn, TA)
    seed_signal(conn, TA, "hi")
    conn.execute("UPDATE signals SET resolved_at = now() WHERE tenant_id = %s", (TA,))
    assert delivery.deliver_signals(conn, TA) == []


def test_the_tick_caps_at_five_with_an_overflow_line_and_never_repeats(conn, master, poster):
    setup_tenant(conn, TA)
    for i in range(8):
        seed_signal(conn, TA, f"s{i}", minutes=100 - i)
    first = delivery.deliver_signals(conn, TA)
    assert len(first) == delivery.SIGNALS_PER_TICK and poster.n == 5
    trail = poster.calls[-1]["inp"]["blocks"][-1]["elements"][0]["text"]
    assert "+3 more waiting in the cockpit" in trail
    assert "more waiting" not in poster.calls[0]["inp"]["blocks"][-1]["elements"][0]["text"]
    second = delivery.deliver_signals(conn, TA)
    assert [o["ref"] for o in second] == ["signal:s5", "signal:s6", "signal:s7"]
    assert poster.n == 8
    assert delivery.deliver_signals(conn, TA) == []  # nothing left


def test_one_failing_post_does_not_stop_the_rest_of_the_tick(conn, master, monkeypatch):
    setup_tenant(conn, TA)
    for i in range(3):
        seed_signal(conn, TA, f"s{i}", title=f"signal-s{i}", minutes=10 - i)

    class Flaky(Poster):
        def __call__(self, inp, *, client=None, token=None):
            if "signal-s1" in str(inp["blocks"]):
                raise slack_executor.SlackError("ratelimited")
            return super().__call__(inp, token=token)

    monkeypatch.setattr(delivery.slack_executor, "post_message", Flaky())
    out = delivery.deliver_signals(conn, TA)
    assert [o["status"] for o in out] == ["sent", "failed", "sent"]
    ledger = {r["ref"]: r["status"] for r in rows(conn, TA)}
    assert ledger == {"signal:s0": "sent", "signal:s1": "failed", "signal:s2": "sent"}


def test_signals_wait_for_slack_instead_of_being_swallowed_before_it_is_connected(conn, master, poster):
    """PE follow-up (Sprint 3b): a tenant whose Slack is not connected yet must not lose its open signals.

    `undelivered_signals` treats any delivery row except `failed` as "already handled", so a `skipped` row for a
    missing credential would consume the ref for good: on the first tick after deploy every open high/cos.risk
    signal would be marked delivered and never posted once Slack arrives. A missing credential therefore writes
    no ledger row — the dedupe for real sends is untouched, and the backlog is still here on the next tick.
    """
    setup_tenant(conn, TA, token=None)
    for i in range(7):
        seed_signal(conn, TA, f"w{i}", minutes=100 - i)

    first = delivery.deliver_signals(conn, TA)
    assert [o["status"] for o in first] == ["skipped"] * delivery.SIGNALS_PER_TICK
    assert poster.calls == [] and rows(conn, TA) == []

    # Slack is connected (the install stores the token and writes the connections row)
    secrets.put(conn, TA, "slack_bot_token", "xoxb-late")
    conn.execute(
        """INSERT INTO connections (id, tenant_id, source, secret_ref)
           VALUES (%s, %s, 'slack', 'kv:slack_bot_token')
           ON CONFLICT (tenant_id, source) DO UPDATE SET secret_ref = EXCLUDED.secret_ref""",
        (f"{TA}-slack", TA),
    )

    second = delivery.deliver_signals(conn, TA)
    assert [o["ref"] for o in second] == [f"signal:w{i}" for i in range(5)]  # still capped at five per tick
    assert all(o["status"] == "sent" for o in second)
    assert poster.tokens == ["xoxb-late"] * 5
    assert "+2 more waiting in the cockpit" in poster.calls[-1]["inp"]["blocks"][-1]["elements"][0]["text"]

    third = delivery.deliver_signals(conn, TA)
    assert [o["ref"] for o in third] == ["signal:w5", "signal:w6"]
    assert poster.n == 7
    assert delivery.deliver_signals(conn, TA) == []  # and now the ledger really is complete
