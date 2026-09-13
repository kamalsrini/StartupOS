"""Track O — onboarding job chain, the parts that need no Postgres: kinds, handler routing, backoff, finish rules."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from common import jobs as queue
from daemon import jobs


def test_kinds_round_trip():
    assert queue.backfill_kind("linear") == "backfill:linear"
    assert queue.backfill_source("backfill:linear") == "linear"
    assert queue.backfill_source("signals") is None
    assert queue.TAIL_KINDS == ("signals", "context_pack", "chief_of_staff", "morning_pulse")


def test_backfill_sources_match_ingest_modules():
    from ingest import runner

    assert set(queue.BACKFILL_SOURCES) == set(runner.SOURCES)


def test_handler_routing():
    assert jobs.handler_for("backfill:brex") is jobs.run_backfill
    assert jobs.handler_for("signals") is jobs.run_signals
    assert jobs.handler_for("context_pack") is jobs.run_context_pack
    assert callable(jobs.handler_for("chief_of_staff")) and callable(jobs.handler_for("morning_pulse"))
    with pytest.raises(KeyError):
        jobs.handler_for("teleport")


def test_backoff_grows_then_caps(monkeypatch):
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (15, 60))
    assert [jobs.backoff_for(n) for n in (1, 2, 3, 9)] == [15, 60, 60, 60]
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", ())
    assert jobs.backoff_for(1) == 0


class FakeConn:
    def __init__(self):
        self.sql: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        return self


def test_finish_done_requeue_failed(monkeypatch):
    monkeypatch.setattr(jobs, "BACKOFF_SECONDS", (15, 60))
    c = FakeConn()
    assert jobs.finish(c, {"id": 1, "attempts": 1}, error=None, result={"issues": 9}) == "done"
    assert "status = 'done'" in c.sql[-1][0]
    assert jobs.finish(c, {"id": 1, "attempts": 1}, error="boom") == "queued"
    sql, params = c.sql[-1]
    assert "status = 'queued'" in sql and "run_after" in sql
    now, run_after = params[1], params[2]
    assert (run_after - now).total_seconds() == 15 and now.tzinfo is UTC
    assert jobs.finish(c, {"id": 1, "attempts": 2}, error="boom") == "queued"
    assert (c.sql[-1][1][2] - c.sql[-1][1][1]).total_seconds() == 60
    assert jobs.finish(c, {"id": 1, "attempts": jobs.MAX_ATTEMPTS}, error="boom") == "failed"
    assert "status = 'failed'" in c.sql[-1][0]
    assert jobs.MAX_ATTEMPTS == 3


def test_public_shape():
    row = {
        "id": 7,
        "kind": "backfill:linear",
        "status": "done",
        "attempts": 1,
        "error": None,
        "payload": {"source": "linear", "result": {"issues": {"created": 9}}},
        "created_at": datetime(2026, 9, 12, 7, 0, tzinfo=UTC),
        "started_at": None,
        "finished_at": None,
    }
    assert queue.public(row) == {
        "id": 7,
        "kind": "backfill:linear",
        "status": "done",
        "attempts": 1,
        "error": None,
        "source": "linear",
        "result": {"issues": {"created": 9}},
        "created_at": "2026-09-12T07:00:00+00:00",
        "started_at": None,
        "finished_at": None,
    }
