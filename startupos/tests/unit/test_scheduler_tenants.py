"""Track T — scheduler tenant iteration (no Postgres): job ids per (job, tenant), refresh_tenants add/remove,
the fallback loop iterating tenants, and the Slack gateway's tenant resolution rules. Track O adds the
process-wide `service_jobs` job (one for all tenants, every 15 s)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from daemon import scheduler
from daemon.gateway import slack as gateway

JOBS = (
    "chief_of_staff",
    "morning_pulse",
    "evening_digest",
    "context_pack",
    "tick_15m",
    "service_asks",
    "weekly_review",
)


def cadence(tz="UTC", pulse_hour=7):
    return {"timezone": tz, "pulse_hour": pulse_hour, "pulse_channel": "web"}


def tenant(tid, tz="UTC", pulse_hour=7):
    return {"id": tid, "name": tid, **cadence(tz, pulse_hour)}


PROCESS_JOBS = {scheduler.REFRESH_TENANTS_ID, scheduler.SERVICE_JOBS_ID}


def ids(sched) -> set[str]:
    return {j.id for j in sched.get_jobs()}


@pytest.fixture(autouse=True)
def no_job_service(monkeypatch):
    """The fallback loop calls service_jobs() every wake; unit tests never touch the DB."""
    calls: list[list[str] | None] = []
    monkeypatch.setattr(scheduler, "service_jobs", lambda tenants=None, *, dsn=None: calls.append(tenants))
    return calls


def test_job_id_round_trip():
    assert scheduler.job_id("morning_pulse", "unitone") == "morning_pulse:unitone"
    assert scheduler.split_job_id("morning_pulse:unitone") == ("morning_pulse", "unitone")
    assert scheduler.split_job_id("refresh_tenants") is None
    assert scheduler.split_job_id("service_jobs") is None


def test_build_scheduler_registers_one_job_per_tenant_plus_refresh(monkeypatch):
    monkeypatch.setattr(scheduler, "discover_tenants", lambda dsn=None: [tenant("a"), tenant("b", "Europe/Berlin", 9)])
    sched = scheduler.build_scheduler()
    assert sched is not None
    expected = {f"{j}:{t}" for j in JOBS for t in ("a", "b")} | PROCESS_JOBS
    assert ids(sched) == expected
    assert scheduler.scheduled_tenants(sched) == {"a", "b"}
    # each tenant's cron is in its own timezone and pulse hour; the refresh job is an interval
    trig = {j.id: str(j.trigger) for j in sched.get_jobs()}
    assert "hour='7'" in trig["morning_pulse:a"] and "hour='9'" in trig["morning_pulse:b"]
    assert "hour='8'" in trig["chief_of_staff:b"] and "minute='30'" in trig["chief_of_staff:b"]
    assert str(sched.get_job("morning_pulse:b").trigger.timezone) == "Europe/Berlin"
    assert "interval[0:05:00]" in trig[scheduler.REFRESH_TENANTS_ID]
    # Track O: ONE job service for the whole process, every 15 s — not one per tenant
    assert "interval[0:00:15]" in trig[scheduler.SERVICE_JOBS_ID]
    assert not any(j.id.startswith("service_jobs:") for j in sched.get_jobs())


def test_pinned_tenant_has_no_refresh_job(monkeypatch):
    monkeypatch.setattr(scheduler, "tenant_cadence", lambda t: cadence())
    sched = scheduler.build_scheduler(["only"])
    assert ids(sched) == {f"{j}:only" for j in JOBS} | {scheduler.SERVICE_JOBS_ID}


def test_pinned_scheduler_services_only_the_pinned_tenants_jobs(monkeypatch, no_job_service):
    monkeypatch.setattr(scheduler, "tenant_cadence", lambda t: cadence())
    sched = scheduler.build_scheduler(["only"])
    sched.get_job(scheduler.SERVICE_JOBS_ID).func()
    assert no_job_service == [["only"]]
    monkeypatch.setattr(scheduler, "discover_tenants", lambda dsn=None: [tenant("a"), tenant("b")])
    scheduler.build_scheduler().get_job(scheduler.SERVICE_JOBS_ID).func()
    assert no_job_service == [["only"], None]  # unpinned: every tenant


def test_refresh_adds_new_tenants_and_removes_gone_ones(monkeypatch):
    monkeypatch.setattr(scheduler, "discover_tenants", lambda dsn=None: [tenant("a")])
    sched = scheduler.build_scheduler()
    assert scheduler.scheduled_tenants(sched) == {"a"}

    out = scheduler.refresh_tenants(sched, [tenant("a"), tenant("b")])
    assert out == {"added": ["b"], "removed": [], "rescheduled": [], "active": ["a", "b"]}
    assert scheduler.scheduled_tenants(sched) == {"a", "b"}
    assert scheduler.REFRESH_TENANTS_ID in ids(sched)

    out = scheduler.refresh_tenants(sched, [tenant("b")])  # a suspended / deleted
    assert out == {"added": [], "removed": ["a"], "rescheduled": [], "active": ["b"]}
    assert scheduler.scheduled_tenants(sched) == {"b"}
    assert not any(j.id.endswith(":a") for j in sched.get_jobs())
    assert scheduler.REFRESH_TENANTS_ID in ids(sched)  # never removed

    # idempotent
    assert scheduler.refresh_tenants(sched, [tenant("b")]) == {
        "added": [],
        "removed": [],
        "rescheduled": [],
        "active": ["b"],
    }
    assert len(sched.get_jobs()) == len(JOBS) + len(PROCESS_JOBS)

    # a cadence edit (onboarding sets pulse_hour/timezone later) re-registers that tenant's jobs in place
    out = scheduler.refresh_tenants(sched, [tenant("b", "Europe/Berlin", 9)])
    assert out["rescheduled"] == ["b"] and out["added"] == [] and out["removed"] == []
    job = sched.get_job("morning_pulse:b")
    assert "hour='9'" in str(job.trigger) and str(job.trigger.timezone) == "Europe/Berlin"
    assert "hour='8'" in str(sched.get_job("chief_of_staff:b").trigger)
    assert len(sched.get_jobs()) == len(JOBS) + len(PROCESS_JOBS)


def test_refresh_never_unschedules_on_a_failed_discovery(monkeypatch):
    monkeypatch.setattr(scheduler, "discover_tenants", lambda dsn=None: [tenant("a")])
    sched = scheduler.build_scheduler()
    monkeypatch.setattr(scheduler, "discover_tenants", lambda dsn=None: [])  # DB unreachable → []
    assert scheduler.refresh_tenants(sched) == {"added": [], "removed": [], "rescheduled": [], "active": ["a"]}
    assert scheduler.scheduled_tenants(sched) == {"a"}


def test_discover_tenants_swallows_db_errors(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(scheduler, "get_conn", boom)
    assert scheduler.discover_tenants("postgresql://nowhere") == []


def test_simple_loop_runs_every_tenants_jobs_in_their_own_timezone(monkeypatch, no_job_service):
    calls: list[tuple[str, str]] = []

    def fake_table(tid, cad=None):
        # one job that always matches (every minute) and one hour-gated on the tenant-local hour
        return [
            {"id": "tick", "cron": {"minute": "*/1"}, "fn": lambda: calls.append(("tick", tid))},
            {"id": "pulse", "cron": {"hour": 7, "minute": 0}, "fn": lambda: calls.append(("pulse", tid))},
        ]

    monkeypatch.setattr(scheduler, "job_table", fake_table)
    monkeypatch.setattr(
        scheduler, "discover_tenants", lambda dsn=None: [tenant("a", "UTC"), tenant("b", "Asia/Kolkata")]
    )

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 12, 1, 30, tzinfo=UTC).astimezone(tz) if tz else datetime(2026, 9, 12, 1, 30)

    monkeypatch.setattr(scheduler, "datetime", FakeDT)
    scheduler.simple_loop(once=True, sleep=lambda _s: None)
    # 01:30 UTC is 07:00 in Kolkata: tenant b's pulse fires, tenant a's does not; the tick runs for both
    assert sorted(calls) == [("pulse", "b"), ("tick", "a"), ("tick", "b")]
    assert no_job_service == [None]  # and the job service ran once, for every tenant


def test_simple_loop_isolates_one_tenants_failure(monkeypatch, no_job_service):
    calls: list[str] = []

    def fake_table(tid, cad=None):
        def fn():
            if tid == "bad":
                raise RuntimeError("boom")
            calls.append(tid)

        return [{"id": "tick", "cron": {"minute": "*/1"}, "fn": fn}]

    monkeypatch.setattr(scheduler, "job_table", fake_table)
    scheduler.simple_loop(["bad", "good"], once=True, sleep=lambda _s: None)
    assert calls == ["good"]
    assert no_job_service == [["bad", "good"]]  # pinned loop: the job service is narrowed the same way


# --- Slack gateway tenant resolution -------------------------------------------------------------------


def test_gateway_resolves_tenant_only_through_the_slack_mapping(monkeypatch):
    monkeypatch.delenv("STARTUPOS_DEV", raising=False)
    mapped = {"U_B": ("tenant-b", "user-b")}
    resolver = lambda u: mapped.get(u)  # noqa: E731
    assert gateway.resolve_event_tenant("U_B", resolver=resolver) == ("tenant-b", "user-b")
    assert gateway.resolve_event_tenant("U_UNKNOWN", resolver=resolver) is None
    assert gateway.resolve_event_tenant("", resolver=resolver) is None
    assert "link" in gateway.link_hint().lower()


def test_gateway_dev_fallback_only_when_dev_tenant_is_given():
    resolver = lambda u: None  # noqa: E731
    assert gateway.resolve_event_tenant("U_X", resolver=resolver, dev_tenant="unitone") == ("unitone", "U_X")
    assert gateway.resolve_event_tenant("", resolver=resolver, dev_tenant="unitone") == ("unitone", "slack")
    # a mapped user wins over the dev fallback
    assert gateway.resolve_event_tenant("U_B", resolver=lambda u: ("tenant-b", "user-b"), dev_tenant="unitone") == (
        "tenant-b",
        "user-b",
    )


@pytest.mark.parametrize("dev", ["", "0", "yes"])
def test_dev_mode_is_only_the_literal_one(monkeypatch, dev):
    from common.tenants import dev_mode

    monkeypatch.setenv("STARTUPOS_DEV", dev)
    assert dev_mode() is False
    monkeypatch.setenv("STARTUPOS_DEV", "1")
    assert dev_mode() is True
