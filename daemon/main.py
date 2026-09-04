"""Daemon entry point.

python -m daemon.main                     # scheduler (+ Slack gateway when tokens exist), runs until SIGINT
python -m daemon.main --once <skill>      # run one skill and exit (ops / smoke tests)
python -m daemon.main --once ask.answer --question "who paid us in June?"
python -m daemon.main --tick              # one 15-minute tick (signal skills + approved executors) and exit
python -m daemon.main --list              # list registered skills
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from common.settings import settings
from daemon import scheduler, skills
from daemon.gateway import slack as slack_gateway

log = logging.getLogger("daemon.main")


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="daemon", description="StartupOS agent daemon")
    p.add_argument("--once", metavar="SKILL", help="run one skill and exit")
    p.add_argument("--question", help="question for ask.answer with --once")
    p.add_argument("--tick", action="store_true", help="run one 15-minute tick and exit")
    p.add_argument("--list", action="store_true", help="list skills")
    p.add_argument("--tenant", default=settings.tenant_id)
    p.add_argument("--no-slack", action="store_true", help="do not start the Slack gateway")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = _parse(argv)

    if args.list:
        for s in sorted(skills.REGISTRY.values(), key=lambda s: s.name):
            print(f"{s.name:28s} T{s.tier} {s.trigger:9s} {s.module:10s} {s.description}")
        return 0

    if args.once:
        extra = {"question": args.question} if args.question else {}
        out = scheduler.run_skill(args.once, args.tenant, **extra)
        if isinstance(out, list):
            for a in out:
                print(f"{a.status:9s} {a.id} · {a.type} · {a.target}")
            print(f"{len(out)} proposals")
        else:
            print(out)
        return 0

    if args.tick:
        scheduler.tick_15m(args.tenant)
        return 0

    sched = scheduler.build_scheduler(args.tenant)
    gateway = None if args.no_slack else slack_gateway.start(args.tenant)
    stop = {"flag": False}

    def _stop(*_: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if sched is None:
        log.warning("apscheduler not installed; using the simple loop")
        try:
            scheduler.simple_loop(args.tenant)
        except KeyboardInterrupt:
            pass
        return 0

    sched.start()
    log.info(
        "scheduler started for tenant %s (%d jobs); slack gateway %s",
        args.tenant,
        len(sched.get_jobs()),
        "on" if gateway else "off",
    )
    try:
        while not stop["flag"]:
            time.sleep(1)
    finally:
        sched.shutdown(wait=False)
        if gateway is not None:
            gateway.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
