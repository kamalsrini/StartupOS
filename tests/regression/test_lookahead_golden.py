"""Golden: the exact lookahead facts computed from tests/fixtures at a frozen `now` (2026-09-04T12:00:00Z).

Same shape as test_signals_golden: the ctx comes from the from_fixture() mappers plus the brain repo docs
(gtm.md front matter, customers.md milestones), so no Postgres is needed. `lookahead.compute(conn, …)` is the
same function over rows loaded from the DB (covered by tests/unit/test_chief_of_staff.py).

Regenerate deliberately with:  STARTUPOS_UPDATE_GOLDEN=1 python -m pytest tests/regression -m regression
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from brain.sync import REPO_DIR, read_repo
from ingest import brex, linear, vercel
from signals import lookahead
from signals.engine import build_ctx

pytestmark = pytest.mark.regression

GOLDEN = Path(__file__).parent / "golden" / "lookahead_fixtures.json"
FROZEN_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def fixture_ctx() -> dict:
    issues, _projects = linear.from_fixture()
    brex_data = brex.from_fixture()
    docs = {d["slice"]: d for d in read_repo(REPO_DIR)}
    ctx = build_ctx(
        issues=issues,
        bills=brex_data["bills"],
        accounts_bank=brex_data["accounts_bank"],
        transactions=brex_data["transactions"],
        deployments=vercel.from_fixture(),
        connections=[{"source": s, "status": "connected", "config": {}} for s in ("brex", "linear", "slack", "vercel")]
        + [{"source": "posthog", "status": "disabled", "config": {}}],
        gtm=docs["gtm"]["meta"],
    )
    ctx["customers_md"] = docs["customers"].get("content") or docs["customers"].get("body") or ""
    ctx["approvals"] = [
        {
            "id": "assign-acm-158",
            "module": "build",
            "type": "Linear · assign",
            "target": "ACM-158 → Alex Dev",
            "status": "pending",
            "created_at": datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        }
    ]
    ctx["budget"] = {
        "month": FROZEN_NOW.date().replace(day=1),
        "state": "normal",
        "tier2_tokens_used": 120_000,
        "tier2_tokens_allowed": 1_500_000,
        "cost_usd": "1.23",
    }
    return ctx


def current() -> dict:
    return lookahead.facts(fixture_ctx(), FROZEN_NOW)


def test_lookahead_from_fixtures_matches_golden():
    got = current()
    if os.environ.get("STARTUPOS_UPDATE_GOLDEN") == "1" or not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps({"now": FROZEN_NOW.isoformat(), "facts": got}, indent=2) + "\n")
    expected = json.loads(GOLDEN.read_text())
    assert expected["now"] == FROZEN_NOW.isoformat()
    assert got == expected["facts"], "lookahead facts changed — review, then regenerate with STARTUPOS_UPDATE_GOLDEN=1"


def test_golden_carries_the_dated_facts_the_cos_relies_on():
    f = json.loads(GOLDEN.read_text())["facts"]
    assert [b["id"] for b in f["cash"]["bills_due"]] == ["expense_b2"]  # b1 CLEARED, b3 beyond 14d
    assert f["cash"]["available"] == "8517.26" and f["cash"]["after_paying_all"] == "2017.26"
    assert f["runway"]["months"] == 1.3 and f["runway"]["basis"] == "outflow_mtd"
    assert [i["id"] for i in f["issues_untouched"]] == ["ACM-126"]
    assert f["deploys"]["failed_last_30d"] == 1 and f["deploys"]["last_prod_deploy"] == "2026-09-02"
    assert f["gtm"]["campaigns"][0]["id"] == "jci-alts-wave-2" and f["gtm"]["campaigns"][0]["age_days"] == 93
    assert [a["id"] for a in f["approvals_stale"]] == ["assign-acm-158"]
    assert f["budget"]["pct"] == 8.0 and f["sources_disconnected"] == [
        {"source": "posthog", "status": "disabled", "last_error": None}
    ]
    heads = lookahead.headlines(f)
    assert heads[0]["title"] == "Runway 1.3 months" and heads[1]["why"].startswith("bill expense_b2")


def test_evaluation_is_deterministic_and_serializable():
    a, b = current(), current()
    assert a == b and json.loads(json.dumps(a)) == a
