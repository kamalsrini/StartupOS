"""Scheduler: the daemon runs a skill only when a schedule ticks, a signal fires, or a person asks.

Jobs (tenant-local time from tenants.timezone), one per (job, tenant) with id "<job_id>:<tenant_id>":
  06:30 cockpit.chief_of_staff (pulse_hour - 1, :30) · 07:00 cockpit.morning_pulse · 18:00 cockpit.evening_digest · every 15 min signal skills + approved executors ·
  Friday 16:00 weekly review placeholder.

Sprint 3a (Track T): `build_scheduler()` registers the job table for every `active_tenants()` row, plus one
process-wide `refresh_tenants` job (every 5 minutes) that adds jobs for tenants that appeared and removes jobs
for tenants that are no longer active — sign-up needs no daemon restart. Every job body opens its own
tenant-bound connection (`get_conn(tenant_id=…)`), so RLS scopes every query to that tenant.

Sprint 3a (Track O): one more process-wide job, `service_jobs` (every 15 s), drains the onboarding chain in
`tenant_jobs` for every tenant (daemon/jobs.py) — it claims across tenants through the SECURITY DEFINER
`tenant_jobs_claim()` and runs each job on that tenant's own connection. A pinned scheduler narrows it to the
pinned tenants.

APScheduler when installed; otherwise a plain minute loop over the same job table and the same tenant list.
`settings.tenant_id` is only the dev/CLI default (`daemon.main --once/--tick`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from common.db import ensure_tenant, get_conn
from common.settings import settings
from common.tenants import CADENCE_DEFAULTS, active_tenants
from daemon import executors, jobs, skills
from daemon.jobs import SERVICE_JOBS_SECONDS
from daemon.skills import build_ctx

log = logging.getLogger("daemon.scheduler")

REFRESH_TENANTS_ID = "refresh_tenants"
REFRESH_TENANTS_MINUTES = 5
SERVICE_JOBS_ID = "service_jobs"
PROCESS_JOB_IDS = (REFRESH_TENANTS_ID, SERVICE_JOBS_ID)


def tenant_cadence(tenant_id: str) -> dict[str, Any]:
    """timezone + pulse_hour from tenants (CONTRACTS.md "Tenant cadence"); safe defaults if the DB is down."""
    try:
        with get_conn(tenant_id=tenant_id) as conn:  # bound to the tenant: RLS shows only its own row
            row = conn.execute(
                "SELECT timezone, pulse_hour, pulse_channel FROM tenants WHERE id = %s", (tenant_id,)
            ).fetchone()
            row = row or {}
            return {
                "timezone": row.get("timezone") or CADENCE_DEFAULTS["timezone"],
                "pulse_hour": int(row.get("pulse_hour") or CADENCE_DEFAULTS["pulse_hour"]),
                "pulse_channel": row.get("pulse_channel") or CADENCE_DEFAULTS["pulse_channel"],
            }
    except Exception:  # DB not reachable at boot → defaults; the job itself will fail loudly later
        return dict(CADENCE_DEFAULTS)


def tenant_timezone(tenant_id: str) -> str:
    return tenant_cadence(tenant_id)["timezone"]


# --- job bodies (each opens its own connection; commit on success, rollback on error) -----------------


def run_skill(name: str, tenant_id: str | None = None, **extra: Any) -> Any:
    tenant_id = tenant_id or settings.tenant_id
    skill = skills.get(name)
    with get_conn(tenant_id=tenant_id) as conn:
        ensure_tenant(conn, tenant_id)  # idempotent; the daemon must never fail on a fresh database
        ctx = build_ctx(conn, tenant_id, **extra)
        out = skill.run(ctx)
    log.info("ran %s for %s → %s", name, tenant_id, _summ(out))
    return out


def run_signal_skills(tenant_id: str | None = None) -> dict[str, Any]:
    """Signal-triggered skills, each in its own transaction so one failure never blocks the others."""
    tenant_id = tenant_id or settings.tenant_id
    results: dict[str, Any] = {}
    for name in skills.SIGNAL_SKILLS:
        try:
            results[name] = run_skill(name, tenant_id)
        except Exception as exc:  # keep the loop alive; the runs ledger has the detail
            log.exception("skill %s failed: %s", name, type(exc).__name__)
            results[name] = exc
    return results


def run_approved(tenant_id: str | None = None) -> list[dict[str, Any]]:
    tenant_id = tenant_id or settings.tenant_id
    with get_conn(tenant_id=tenant_id) as conn:
        done = executors.run_all_approved(conn, tenant_id)
    if done:
        log.info("executed %d approvals for %s", len(done), tenant_id)
    return done


def run_signal_engine(tenant_id: str | None = None) -> dict[str, Any]:
    """Evaluate the Tier-0 rules over ingested state (spec audit 2026-09-12: this was never scheduled in
    production, so the deployed daemon proposed nothing). Runs first in every 15-minute tick."""
    tenant_id = tenant_id or settings.tenant_id
    from signals import engine as signal_engine

    with get_conn(tenant_id=tenant_id) as conn:
        out = signal_engine.run(conn, tenant_id)
    log.info(
        "signals for %s: %s",
        tenant_id,
        {k: v for k, v in out.items() if k in ("created", "updated", "resolved", "open")},
    )
    return out


def compile_context_pack(tenant_id: str | None = None) -> str:
    """Nightly context pack (Architecture Brief §2.1) — one deterministic compile; Tier-1 summarize is opt-in."""
    tenant_id = tenant_id or settings.tenant_id
    from brain import pack as brain_pack

    with get_conn(tenant_id=tenant_id) as conn:
        content = brain_pack.compile(conn, tenant_id)
    log.info("context pack for %s: ~%d tokens", tenant_id, len(content) // 4)
    return content


def tick_15m(tenant_id: str | None = None) -> None:
    try:
        run_signal_engine(tenant_id)
    except Exception as exc:  # signals failing must not stop skills/executors
        log.exception("signal engine failed: %s", type(exc).__name__)
    run_signal_skills(tenant_id)
    run_approved(tenant_id)


def service_asks(tenant_id: str | None = None, limit: int = 5) -> int:
    """Answer pending rows in `asks` (queued by the API, which never calls a model). Runs every 30s."""
    tenant_id = tenant_id or settings.tenant_id
    from daemon import skills as _skills
    from daemon.skills.base import build_ctx

    done = 0
    with get_conn(tenant_id=tenant_id) as conn:
        rows = conn.execute(
            """UPDATE asks SET status='running' WHERE id IN (
                 SELECT id FROM asks WHERE tenant_id=%s AND status='pending' ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED
               ) RETURNING id, user_id, mode, question""",
            (tenant_id, limit),
        ).fetchall()
        conn.commit()
        for row in rows:
            try:
                if row["mode"] == "cos":
                    from daemon.skills.cockpit import chief_of_staff as cos

                    out = cos.run(build_ctx(conn, tenant_id, trigger="ask", question=row["question"]))
                    run_id = out.get("run_id")
                    answer = {k: v for k, v in out.items() if k not in ("signals", "approvals")}
                else:
                    from daemon.skills.ask import answer as ask_answer

                    ctx = build_ctx(conn, tenant_id, question=row["question"])
                    text = ask_answer.answer(ctx, row["question"])
                    run_id = ctx.get("run_id")
                    answer = {"text": text}
                if run_id:
                    conn.execute("UPDATE runs SET acted_by=%s WHERE id=%s", (row["user_id"], run_id))
                conn.execute(
                    "UPDATE asks SET status='done', answer=%s, run_id=%s, answered_at=now() WHERE id=%s",
                    (Jsonb(answer), run_id, row["id"]),
                )
                done += 1
            except Exception as exc:  # one bad ask never blocks the queue
                log.exception("ask %s failed: %s", row["id"], type(exc).__name__)
                conn.execute(
                    "UPDATE asks SET status='failed', answer=%s, answered_at=now() WHERE id=%s",
                    (Jsonb({"error": type(exc).__name__}), row["id"]),
                )
            conn.commit()
    _ = _skills
    return done


def service_jobs(tenants: list[str] | None = None, *, dsn: str | None = None) -> dict[str, Any]:
    """Drain the onboarding job chain (tenant_jobs) — every tenant unless `tenants` pins some. Every 15 s."""
    out = jobs.service_jobs(tenants, dsn=dsn)
    if out["claimed"]:
        log.info("serviced %d onboarding jobs: %s", out["claimed"], {k: out[k] for k in ("done", "queued", "failed")})
    return out


def weekly_review(tenant_id: str | None = None) -> str:
    """Friday 16:00 placeholder — Sales and Build weekly review lands in v1.1 (Architecture Brief §5.2)."""
    tenant_id = tenant_id or settings.tenant_id
    with get_conn(tenant_id=tenant_id) as conn:
        from daemon import llm

        llm.record_tier0(conn, tenant_id, "cockpit.weekly_review", "schedule", "placeholder: not implemented in v1")
    return "weekly review placeholder"


def _summ(out: Any) -> str:
    if isinstance(out, list):
        return f"{len(out)} proposals"
    if isinstance(out, str):
        return f"{len(out)} chars"
    return type(out).__name__


# --- job table ------------------------------------------------------------------------------------


def job_table(tenant_id: str, cadence: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Declarative job list shared by APScheduler and the fallback loop. Ids are unqualified here; the scheduler
    registers them as job_id(id, tenant_id)."""
    pulse_hour = int((cadence or tenant_cadence(tenant_id))["pulse_hour"])
    # The Chief of Staff runs 30 minutes before the pulse so the pulse can read its brief: pulse_hour-1 at :30
    # (7 → 06:30). A midnight pulse (0) keeps the default 06:30 rather than wrapping to the previous day.
    cos_hour = pulse_hour - 1 if pulse_hour > 0 else 6
    return [
        {
            "id": "chief_of_staff",
            "cron": {"hour": cos_hour, "minute": 30},
            "fn": lambda: run_skill("cockpit.chief_of_staff", tenant_id),
        },
        {
            "id": "morning_pulse",
            "cron": {"hour": pulse_hour, "minute": 0},
            "fn": lambda: run_skill("cockpit.morning_pulse", tenant_id),
        },
        {
            "id": "evening_digest",
            "cron": {"hour": 18, "minute": 0},
            "fn": lambda: run_skill("cockpit.evening_digest", tenant_id),
        },
        {
            "id": "context_pack",
            "cron": {"hour": 2, "minute": 0},
            "fn": lambda: compile_context_pack(tenant_id),
        },
        {"id": "tick_15m", "cron": {"minute": "*/15"}, "fn": lambda: tick_15m(tenant_id)},
        {"id": "service_asks", "cron": {"second": "*/30"}, "fn": lambda: service_asks(tenant_id)},
        {
            "id": "weekly_review",
            "cron": {"day_of_week": "fri", "hour": 16, "minute": 0},
            "fn": lambda: weekly_review(tenant_id),
        },
    ]


def job_id(job: str, tenant_id: str) -> str:
    """APScheduler id for one (job, tenant): "morning_pulse:unitone"."""
    return f"{job}:{tenant_id}"


def split_job_id(jid: str) -> tuple[str, str] | None:
    """Inverse of job_id; None for process-wide jobs (refresh_tenants)."""
    job, sep, tenant = jid.partition(":")
    return (job, tenant) if sep and tenant else None


def scheduled_tenants(sched: Any) -> set[str]:
    """Tenants that currently have jobs registered on `sched`."""
    out: set[str] = set()
    for j in sched.get_jobs():
        parts = split_job_id(j.id)
        if parts:
            out.add(parts[1])
    return out


def discover_tenants(dsn: str | None = None) -> list[dict[str, Any]]:
    """active_tenants() on an anonymous service connection. Empty (and logged) when the DB is unreachable."""
    try:
        with get_conn(dsn, tenant_id="") as anon:
            return active_tenants(anon)
    except Exception as exc:
        log.warning("could not list active tenants: %s", type(exc).__name__)
        return []


def add_tenant_jobs(sched: Any, tenant_id: str, cadence: dict[str, Any] | None = None) -> list[str]:
    """Register the job table for one tenant in its own timezone. Existing ids are replaced (idempotent)."""
    from apscheduler.triggers.cron import CronTrigger

    cadence = cadence or tenant_cadence(tenant_id)
    remove_tenant_jobs(sched, tenant_id)  # explicit: replace_existing does not dedupe pending jobs before start()
    tz = ZoneInfo(cadence.get("timezone") or CADENCE_DEFAULTS["timezone"])
    ids: list[str] = []
    for job in job_table(tenant_id, cadence):
        jid = job_id(job["id"], tenant_id)
        sched.add_job(job["fn"], CronTrigger(timezone=tz, **job["cron"]), id=jid, name=jid, replace_existing=True)
        ids.append(jid)
    return ids


def remove_tenant_jobs(sched: Any, tenant_id: str) -> list[str]:
    removed: list[str] = []
    for j in list(sched.get_jobs()):
        parts = split_job_id(j.id)
        if parts and parts[1] == tenant_id:
            sched.remove_job(j.id)
            removed.append(j.id)
    return removed


def refresh_tenants(sched: Any, tenants: list[dict[str, Any]] | None = None, *, dsn: str | None = None) -> dict:
    """Reconcile the scheduler with active_tenants(): add jobs for new tenants, drop jobs for gone ones.

    Runs every REFRESH_TENANTS_MINUTES as its own job, so a tenant created through the API gets its pulse
    without a daemon restart. `tenants` is injectable for tests; None → discover from the DB. A failed discovery
    (empty list because the DB was unreachable) removes nothing: we never unschedule on a transient error.
    """
    if tenants is None:
        tenants = discover_tenants(dsn)
        if not tenants:
            return {"added": [], "removed": [], "rescheduled": [], "active": sorted(scheduled_tenants(sched))}
    wanted = {t["id"]: t for t in tenants}
    have = scheduled_tenants(sched)
    added = [tid for tid in wanted if tid not in have]
    removed = [tid for tid in have if tid not in wanted]
    # cadence edits (onboarding sets pulse_hour/timezone after the tenant exists): re-register those jobs
    rescheduled = [
        tid for tid in wanted if tid in have and _cadence_key(wanted[tid]) != _registered_cadence(sched, tid)
    ]
    for tid in added + rescheduled:
        add_tenant_jobs(sched, tid, wanted[tid])
    for tid in removed:
        remove_tenant_jobs(sched, tid)
    if added or removed or rescheduled:
        log.info("tenants refreshed: +%s -%s ~%s", added, removed, rescheduled)
    return {"added": added, "removed": removed, "rescheduled": rescheduled, "active": sorted(wanted)}


def _cadence_key(cadence: dict[str, Any]) -> tuple[str, int]:
    return (str(cadence.get("timezone") or CADENCE_DEFAULTS["timezone"]), int(cadence.get("pulse_hour") or 7))


def _registered_cadence(sched: Any, tenant_id: str) -> tuple[str, int] | None:
    """(timezone, pulse_hour) as currently registered, read back from the morning_pulse trigger."""
    job = sched.get_job(job_id("morning_pulse", tenant_id))
    if job is None:
        return None
    trig = job.trigger
    hour = next((f for f in trig.fields if f.name == "hour"), None)
    try:
        return (str(trig.timezone), int(str(hour)))
    except (TypeError, ValueError):
        return None


def build_scheduler(tenants: str | list[str] | list[dict[str, Any]] | None = None, *, dsn: str | None = None) -> Any:
    """Return a configured (not started) APScheduler BackgroundScheduler, or None if apscheduler is missing.

    `tenants` None → every active tenant from the DB (production). A tenant id / list of ids pins the set
    (dev: `daemon.main --tenant`). One job per (job, tenant) plus the process-wide refresh_tenants job.
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        return None
    sched = BackgroundScheduler(
        timezone=ZoneInfo("UTC"), job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600}
    )
    pinned = tenants is not None
    rows = _tenant_rows(tenants, dsn)
    for t in rows:
        add_tenant_jobs(sched, t["id"], t if "timezone" in t else None)
    if not pinned:
        sched.add_job(
            lambda: refresh_tenants(sched, dsn=dsn),
            IntervalTrigger(minutes=REFRESH_TENANTS_MINUTES),
            id=REFRESH_TENANTS_ID,
            name=REFRESH_TENANTS_ID,
        )
    # Track O: the onboarding job service is ONE job for the whole process (it claims across tenants itself);
    # a pinned (dev) scheduler services only the pinned tenants' queues.
    pinned_ids = [t["id"] for t in rows] if pinned else None
    sched.add_job(
        lambda: service_jobs(pinned_ids, dsn=dsn),
        IntervalTrigger(seconds=SERVICE_JOBS_SECONDS),
        id=SERVICE_JOBS_ID,
        name=SERVICE_JOBS_ID,
    )
    return sched


def _tenant_rows(tenants: str | list[str] | list[dict[str, Any]] | None, dsn: str | None) -> list[dict[str, Any]]:
    if tenants is None:
        return discover_tenants(dsn)
    if isinstance(tenants, str):
        tenants = [tenants]
    return [t if isinstance(t, dict) else {"id": t} for t in tenants]


def _matches(cron: dict[str, Any], now: datetime) -> bool:
    minute = cron.get("minute", 0)
    if isinstance(minute, str) and minute.startswith("*/"):
        if now.minute % int(minute[2:]) != 0:
            return False
    elif now.minute != int(minute):
        return False
    if "hour" in cron and now.hour != int(cron["hour"]):
        return False
    if "day_of_week" in cron and now.strftime("%a").lower() != str(cron["day_of_week"]).lower():
        return False
    return True


def simple_loop(
    tenants: str | list[str] | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    once: bool = False,
    dsn: str | None = None,
) -> None:
    """Fallback when apscheduler is unavailable: wake every minute, run each tenant's jobs whose cron matches
    in that tenant's timezone. The tenant list is re-read every REFRESH_TENANTS_MINUTES unless pinned. The
    onboarding job service runs on every wake (≈ every 20 s) for the same tenants."""
    pinned = tenants is not None
    rows = _tenant_rows(tenants, dsn)
    last_refresh = time.monotonic()
    last_minute: dict[str, datetime] = {}
    while True:
        if not pinned and time.monotonic() - last_refresh >= REFRESH_TENANTS_MINUTES * 60:
            rows = discover_tenants(dsn) or rows
            last_refresh = time.monotonic()
        for t in rows:
            tid = t["id"]
            cadence = t if "timezone" in t else tenant_cadence(tid)
            tz = ZoneInfo(cadence.get("timezone") or CADENCE_DEFAULTS["timezone"])
            now = datetime.now(UTC).astimezone(tz).replace(second=0, microsecond=0)
            if now == last_minute.get(tid):
                continue
            last_minute[tid] = now
            for job in job_table(tid, cadence):
                if _matches(job["cron"], now):
                    try:
                        job["fn"]()
                    except Exception as exc:
                        log.exception("job %s failed: %s", job_id(job["id"], tid), type(exc).__name__)
        try:
            service_jobs([t["id"] for t in rows] if pinned else None, dsn=dsn)
        except Exception as exc:  # never let the queue take the loop down
            log.exception("service_jobs failed: %s", type(exc).__name__)
        if once:
            return
        sleep(20)
