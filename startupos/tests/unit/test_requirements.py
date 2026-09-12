"""Every third-party import in the runtime packages must be declared in requirements.txt (deploy parity)."""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIRS = ["api", "auth", "brain", "common", "daemon", "ingest", "signals", "scripts"]
# import name → distribution name in requirements.txt
ALIASES = {
    "jwt": "PyJWT",
    "psycopg": "psycopg",
    "dotenv": "python-dotenv",
    "slack_sdk": "slack_sdk",
    "apscheduler": "APScheduler",
}


def declared() -> set[str]:
    out = set()
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.add(re.split(r"[\[=<>]", line)[0].lower())
    return out


def top_level_imports() -> set[str]:
    names = set()
    for d in RUNTIME_DIRS:
        for f in (ROOT / d).rglob("*.py"):
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names.add(node.module.split(".")[0])
    return names


def test_all_third_party_imports_are_declared():
    stdlib = set(sys.stdlib_module_names)
    local = set(RUNTIME_DIRS) | {"tests", "web"}
    third_party = {n for n in top_level_imports() if n not in stdlib and n not in local}
    req = declared()
    missing = {n for n in third_party if ALIASES.get(n, n).lower() not in req}
    assert not missing, f"imports not in requirements.txt: {sorted(missing)}"
