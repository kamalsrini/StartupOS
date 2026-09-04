"""Skill library. Importing this package registers every v1 skill into `daemon.skills.base.REGISTRY`."""

from __future__ import annotations

from daemon.skills import base
from daemon.skills.ask import answer as _answer
from daemon.skills.base import REGISTRY, Ctx, Skill, build_ctx
from daemon.skills.build import assign_owner as _assign_owner
from daemon.skills.build import nudge_stale as _nudge_stale
from daemon.skills.cockpit import evening_digest as _evening_digest
from daemon.skills.cockpit import morning_pulse as _morning_pulse
from daemon.skills.customers import prioritize_ask as _prioritize_ask
from daemon.skills.finance import ap_queue as _ap_queue

SIGNAL_SKILLS = (
    _assign_owner.NAME,
    _prioritize_ask.NAME,
    _ap_queue.NAME,
    _nudge_stale.NAME,
)
SCHEDULED_SKILLS = (_morning_pulse.NAME, _evening_digest.NAME)
ASK_SKILL = _answer.NAME


def get(name: str) -> Skill:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"unknown skill {name!r}; known: {sorted(REGISTRY)}") from exc


__all__ = ["ASK_SKILL", "REGISTRY", "SCHEDULED_SKILLS", "SIGNAL_SKILLS", "Ctx", "Skill", "base", "build_ctx", "get"]
