"""Golden: the exact set of signal ids + severities the rules produce from tests/fixtures at a frozen `now`.

No Postgres: the ctx is assembled from the same from_fixture() mappers the ingest runner uses, plus the
connections the runner would create and the brain gtm.md front matter.

Regenerate deliberately with:  STARTUPOS_UPDATE_GOLDEN=1 python -m pytest tests/regression -m regression
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.sync import REPO_DIR, read_repo
from ingest import brex, linear, slack, vercel
from signals.engine import build_ctx, evaluate

pytestmark = pytest.mark.regression

GOLDEN = Path(__file__).parent / "golden" / "signals_fixtures.json"
FROZEN_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)  # the freeze-time helper: every rule sees this clock


def fixture_ctx() -> dict:
    issues, _projects = linear.from_fixture()
    brex_data = brex.from_fixture()
    _messages = slack.from_fixture()
    gtm = next(d["meta"] for d in read_repo(REPO_DIR) if d["slice"] == "gtm")
    connections = [{"source": s, "status": "connected", "config": {}} for s in ("brex", "linear", "slack", "vercel")]
    return build_ctx(
        issues=issues,
        bills=brex_data["bills"],
        accounts_bank=brex_data["accounts_bank"],
        transactions=brex_data["transactions"],
        deployments=vercel.from_fixture(),
        connections=connections,
        gtm=gtm,
        social={"linkedin_pending": int(gtm.get("linkedin_queue_pending") or 0)},
    )


def current() -> list[dict]:
    return [{"id": s["id"], "severity": s["severity"]} for s in evaluate(fixture_ctx(), FROZEN_NOW)]


def test_signals_from_fixtures_match_golden():
    got = current()
    if os.environ.get("STARTUPOS_UPDATE_GOLDEN") == "1" or not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps({"now": FROZEN_NOW.isoformat(), "signals": got}, indent=2) + "\n")
    expected = json.loads(GOLDEN.read_text())
    assert expected["now"] == FROZEN_NOW.isoformat()
    assert got == expected["signals"], "signal set changed — review, then regenerate with STARTUPOS_UPDATE_GOLDEN=1"


def test_golden_contains_the_contracted_signals():
    ids = {s["id"] for s in json.loads(GOLDEN.read_text())["signals"]}
    assert {
        "build.unassigned_high:ACM-158",
        "build.stale_in_progress:ACM-126",
        "build.duplicate_titles:ACM-150+ACM-151",
        "finance.bill_due_7d:expense_b2",
        "customers.ask_untouched:ACM-160",
        "build.deploy_failed:dpl_2",
    } <= ids


def test_evaluation_is_deterministic():
    assert current() == current()
