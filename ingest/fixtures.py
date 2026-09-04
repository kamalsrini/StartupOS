"""Load tests/fixtures/*.json. Used when a source has no credential configured (and by tests)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def load(name: str, base: Path | None = None) -> Any:
    path = (base or FIXTURES_DIR) / name
    return json.loads(path.read_text(encoding="utf-8"))
