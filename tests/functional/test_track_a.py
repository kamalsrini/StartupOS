"""Track A end-to-end on Postgres: fixtures → ingest → signals → pack → digest. Frozen now = 2026-09-04T12:00Z."""

from datetime import UTC, datetime, timedelta

import pytest

from brain import pack, retrieve, sync
from ingest import runner
from signals import digest, engine

pytestmark = pytest.mark.functional

TENANT = "unitone"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

REQUIRED_SIGNALS = {
    "build.unassigned_high:ACM-158",
    "build.stale_in_progress:ACM-126",
    "build.duplicate_titles:ACM-150+ACM-151",
    "finance.bill_due_7d:expense_b2",
    "customers.ask_untouched:ACM-160",
    "build.deploy_failed:dpl_2",
}


def count(conn, table):
    return conn.execute(f"SELECT count(*) AS n FROM {table} WHERE tenant_id = %s", (TENANT,)).fetchone()["n"]


def test_track_a_pipeline(conn, monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    # --- ingest from fixtures -------------------------------------------------------------
    results = runner.run_all(conn, TENANT, force_fixtures=True)
    assert set(results) == {"linear", "slack", "brex", "vercel"}
    assert all(tables for tables in results.values()), results
    assert count(conn, "issues") == 9
    assert count(conn, "projects") == 3
    assert count(conn, "messages") == 3
    assert count(conn, "accounts_bank") == 2
    assert count(conn, "bills") == 3
    assert count(conn, "vendors") == 2
    assert count(conn, "cards") == 1
    assert count(conn, "transactions") == 2
    assert count(conn, "deployments") == 2
    assert count(conn, "connections") == 4
    conns = {r["source"]: r for r in conn.execute("SELECT * FROM connections WHERE tenant_id = %s", (TENANT,))}
    assert conns["linear"]["last_sync_at"] is not None and conns["linear"]["last_error"] is None
    assert conns["linear"]["secret_ref"] == "env:LINEAR_API_KEY" and conns["linear"]["config"]["mode"] == "fixtures"
    events_after_first = count(conn, "events")
    assert events_after_first == 9 + 3 + 3 + 2 + 3 + 2 + 1 + 2 + 2

    # vendors: only the allow-listed columns exist, no bank/tax data
    vendor_cols = {
        r["column_name"]
        for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'vendors'")
    }
    assert vendor_cols == {"tenant_id", "id", "name", "email", "rail", "country", "status"}

    # --- ingest is idempotent -------------------------------------------------------------
    again = runner.run_all(conn, TENANT, force_fixtures=True)
    assert all(r.created == 0 and r.updated == 0 for tables in again.values() for r in tables.values())
    assert count(conn, "events") == events_after_first

    # --- a real change produces exactly one 'updated' event with a field diff -------------
    from ingest import linear

    issues, _ = linear.from_fixture()
    changed = [dict(i, assignee="Alex Dev") if i["id"] == "ACM-158" else i for i in issues]
    res = linear.issues_upserter.upsert(conn, TENANT, changed)
    assert res.updated == 1 and res.created == 0 and res.unchanged == 8
    assert res.events[0]["diff"] == {"assignee": [None, "Alex Dev"]}
    linear.issues_upserter.upsert(conn, TENANT, issues)  # restore fixture state
    assert count(conn, "events") == events_after_first + 2

    # --- brain repo → brain_docs ----------------------------------------------------------
    versions = sync.load_repo(conn, TENANT)
    assert set(versions) == {f"{s}.md" for s in sync.SLICES}
    assert all(v == 1 for v in versions.values())
    assert sync.load_repo(conn, TENANT) == versions  # unchanged content → no version bump
    conn.execute(
        "UPDATE brain_docs SET content = content || E'\\n(edited)' WHERE tenant_id = %s AND path = 'gtm.md'", (TENANT,)
    )
    assert sync.load_repo(conn, TENANT)["gtm.md"] == 2  # repo differs from DB → version bump

    # --- retrieval ranks the brain doc about the topic first ------------------------------
    hits = retrieve.search(conn, TENANT, "Honeywell Tridium warm intro", k=5)
    assert hits and hits[0]["source"] == "brain_docs" and hits[0]["path"] in {"customers.md", "icp.md"}
    assert all(hits[i]["score"] >= hits[i + 1]["score"] for i in range(len(hits) - 1))
    slack_hits = retrieve.search(conn, TENANT, "Northwind JSON issue reports", k=8)
    assert any(h["source"] == "messages" for h in slack_hits)
    assert retrieve.search(conn, TENANT, "", k=5) == []

    # --- signals --------------------------------------------------------------------------
    result = engine.run(conn, TENANT, now=NOW)
    fired = {s["id"] for s in result["signals"]}
    assert REQUIRED_SIGNALS <= fired, REQUIRED_SIGNALS - fired
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM signals WHERE tenant_id = %s", (TENANT,))}
    assert REQUIRED_SIGNALS <= set(rows)
    assert all(r["resolved_at"] is None for r in rows.values())
    assert rows["build.unassigned_high:ACM-158"]["severity"] == "high"
    assert rows["build.unassigned_high:ACM-158"]["suggested_skill"] == "build.assign_owner"
    assert rows["build.unassigned_high:ACM-158"]["first_seen_at"] == NOW

    # re-evaluation bumps last_seen_at only; resolution when the rule stops matching
    later = NOW + timedelta(hours=1)
    engine.run(conn, TENANT, now=later)
    row = conn.execute("SELECT * FROM signals WHERE id = %s", ("build.unassigned_high:ACM-158",)).fetchone()
    assert row["first_seen_at"] == NOW and row["last_seen_at"] == later and row["resolved_at"] is None
    conn.execute("UPDATE issues SET assignee = 'Alex Dev' WHERE tenant_id = %s AND id = 'ACM-158'", (TENANT,))
    engine.run(conn, TENANT, now=later + timedelta(hours=1))
    row = conn.execute("SELECT * FROM signals WHERE id = %s", ("build.unassigned_high:ACM-158",)).fetchone()
    assert row["resolved_at"] == later + timedelta(hours=1)
    conn.execute("UPDATE issues SET assignee = NULL WHERE tenant_id = %s AND id = 'ACM-158'", (TENANT,))
    engine.run(conn, TENANT, now=later + timedelta(hours=2))
    row = conn.execute("SELECT * FROM signals WHERE id = %s", ("build.unassigned_high:ACM-158",)).fetchone()
    assert row["resolved_at"] is None and row["first_seen_at"] == NOW  # re-opened, history kept

    # --- context pack ---------------------------------------------------------------------
    content = pack.compile(conn, TENANT, now=NOW)
    stored = pack.latest(conn, TENANT)
    assert stored is not None and stored["content"] == content
    assert stored["token_estimate"] == pack.estimate_tokens(content) <= pack.MAX_TOKENS
    from common.ids import content_key

    assert stored["cache_key"] == content_key(content)
    for title in pack.TITLES.values():
        assert f"\n## {title}\n" in content
    assert "build.unassigned_high:ACM-158" in content and "DevShop LLC" in content
    assert pack.compile(conn, TENANT, now=NOW) == content  # deterministic
    assert count(conn, "context_packs") == 1  # identical pack not stored twice

    # --- digest ---------------------------------------------------------------------------
    text = digest.build(conn, TENANT, now=NOW + timedelta(hours=6))
    assert "high" in text
    assert "*Approvals waiting:* 0" in text
    assert "ACM-158" in text
    assert digest.post_to_slack(text) is False  # no SLACK_BOT_TOKEN in tests → never posts
