"""Scheduler: the daemon runs a skill only when a schedule ticks, a signal fires, or a person asks.

Jobs (tenant-local time from tenants.timezone):
  06:30 cockpit.chief_of_staff (pulse_hour - 1, :30) · 07:00 cockpit.morning_pulse · 18:00 cockpit.evening_digest · every 15 min signal skills + approved executors ·
  Friday 16:00 weekly review placeholder.

APScheduler when installed; otherwise a plain minute loop with the same job table.
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
from daemon import executors, skills
from daemon.skills import build_ctx

log = logging.getLogger("daemon.scheduler")


def tenant_cadence(tenant_id: str) -> dict[str, Any]:
    """timezone + pulse_hour from tenants (CONTRACTS.md "Tenant cadence"); safe defaults if the DB is down."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT timezone, pulse_hour, pulse_channel FROM tenants WHERE id = %s", (tenant_id,)
            ).fetchone()
            row = row or {}
            return {
                "timezone": row.get("timezone") or "America/Los_Angeles",
                "pulse_hour": int(row.get("pulse_hour") or 7),
                "pulse_channel": row.get("pulse_channel") or "web",
            }
    except Exception:  # DB not reachable at boot → defaults; the job itself will fail loudly later
        return {"timezone": "America/Los_Angeles", "pulse_hour": 7, "pulse_channel": "web"}


def tenant_timezone(tenant_id: str) -> str:
    return tenant_cadence(tenant_id)["timezone"]


# --- job bodies (each opens its own connection; commit on success, rollback on error) -----------------


def run_skill(name: str, tenant_id: str | None = None, **extra: Any) -> Any:
    tenant_id = tenant_id or settings.tenant_id
    skill = skills.get(name)
    with get_conn() as conn:
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
    with get_conn() as conn:
        done = executors.run_all_approved(conn, tenant_id)
    if done:
        log.info("executed %d approvals for %s", len(done), tenant_id)
    return done


def tick_15m(tenant_id: str | None = None) -> None:
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


def weekly_review(tenant_id: str | None = None) -> str:
    """Friday 16:00 placeholder — Sales and Build weekly review lands in v1.1 (Architecture Brief §5.2)."""
    tenant_id = tenant_id or settings.tenant_id
    with get_conn() as conn:
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


def job_table(tenant_id: str) -> list[dict[str, Any]]:
    """Declarative job list shared by APScheduler and the fallback loop."""
    pulse_hour = tenant_cadence(tenant_id)["pulse_hour"]
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
        {"id": "tick_15m", "cron": {"minute": "*/15"}, "fn": lambda: tick_15m(tenant_id)},
        {"id": "service_asks", "cron": {"second": "*/30"}, "fn": lambda: service_asks(tenant_id)},
        {
            "id": "weekly_review",
            "cron": {"day_of_week": "fri", "hour": 16, "minute": 0},
            "fn": lambda: weekly_review(tenant_id),
        },
    ]


def build_scheduler(tenant_id: str | None = None) -> Any:
    """Return a configured (not started) APScheduler BackgroundScheduler, or None if apscheduler is missing."""
    tenant_id = tenant_id or settings.tenant_id
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        return None
    tz = ZoneInfo(tenant_timezone(tenant_id))
    sched = BackgroundScheduler(
        timezone=tz, job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600}
    )
    for job in job_table(tenant_id):
        sched.add_job(job["fn"], CronTrigger(timezone=tz, **job["cron"]), id=job["id"], name=job["id"])
    return sched


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
    tenant_id: str | None = None, *, sleep: Callable[[float], None] = time.sleep, once: bool = False
) -> None:
    """Fallback when apscheduler is unavailable: wake every minute, run jobs whose cron matches."""
    tenant_id = tenant_id or settings.tenant_id
    tz = ZoneInfo(tenant_timezone(tenant_id))
    last_minute: datetime | None = None
    while True:
        now = datetime.now(UTC).astimezone(tz).replace(second=0, microsecond=0)
        if now != last_minute:
            last_minute = now
            for job in job_table(tenant_id):
                if _matches(job["cron"], now):
                    try:
                        job["fn"]()
                    except Exception as exc:
                        log.exception("job %s failed: %s", job["id"], type(exc).__name__)
        if once:
            return
        sleep(20)
