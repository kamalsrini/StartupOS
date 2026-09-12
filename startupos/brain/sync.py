"""brain/repo/*.md ⇄ brain_docs.

Front matter is a small YAML subset: one `key: value` per line between `---` fences. Values that parse
as JSON (numbers, quoted strings, lists, objects) are decoded; everything else is a plain string. This is
valid YAML too, so the files stay readable by any YAML tool without adding a dependency here.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

REPO_DIR = Path(__file__).resolve().parent / "repo"
SLICES = ("identity", "icp", "voice", "pricing", "team", "customers", "decisions", "finance", "build", "gtm")


def parse_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split a Markdown file into (front_matter, body). No front matter → ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta: dict[str, Any] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        meta[key.strip()] = _parse_scalar(raw.strip())
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return meta, body


def _parse_scalar(raw: str) -> Any:
    if raw == "":
        return ""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def render_front_matter(meta: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key, val in meta.items():
        if isinstance(val, str):
            lines.append(f"{key}: {val}")
        else:
            lines.append(f"{key}: {json.dumps(val, separators=(', ', ': '))}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def read_repo(path: Path | None = None) -> list[dict[str, Any]]:
    """Read every *.md in the repo dir → [{path, slice, source, content, meta, body}] sorted by path."""
    root = Path(path) if path else REPO_DIR
    out = []
    for file in sorted(root.glob("*.md")):
        content = file.read_text(encoding="utf-8")
        meta, body = parse_front_matter(content)
        slice_ = str(meta.get("slice") or file.stem)
        out.append(
            {
                "path": file.name,
                "slice": slice_,
                "source": str(meta.get("source") or "human"),
                "content": content,
                "meta": meta,
                "body": body,
            }
        )
    return out


def load_repo(conn: psycopg.Connection, tenant_id: str, path: Path | None = None) -> dict[str, int]:
    """Upsert repo files into brain_docs. Version bumps only when content changed. Returns {path: version}."""
    versions: dict[str, int] = {}
    now = datetime.now(UTC)
    for doc in read_repo(path):
        row = conn.execute(
            "SELECT content, version FROM brain_docs WHERE tenant_id = %s AND path = %s",
            (tenant_id, doc["path"]),
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO brain_docs (tenant_id, path, slice, content, version, source, updated_at)
                   VALUES (%s, %s, %s, %s, 1, %s, %s)""",
                (tenant_id, doc["path"], doc["slice"], doc["content"], doc["source"], now),
            )
            versions[doc["path"]] = 1
        elif row["content"] != doc["content"]:
            conn.execute(
                """UPDATE brain_docs SET content = %s, slice = %s, source = %s, version = version + 1, updated_at = %s
                   WHERE tenant_id = %s AND path = %s""",
                (doc["content"], doc["slice"], doc["source"], now, tenant_id, doc["path"]),
            )
            versions[doc["path"]] = row["version"] + 1
        else:
            versions[doc["path"]] = row["version"]
    return versions


def export_repo(conn: psycopg.Connection, tenant_id: str, path: Path) -> list[str]:
    """Write brain_docs rows back to Markdown files under `path`. Returns written paths."""
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    written = []
    rows = conn.execute(
        "SELECT path, content FROM brain_docs WHERE tenant_id = %s ORDER BY path", (tenant_id,)
    ).fetchall()
    for row in rows:
        target = root / row["path"]
        target.write_text(row["content"], encoding="utf-8")
        written.append(str(target))
    return written


def get_docs(conn: psycopg.Connection, tenant_id: str) -> dict[str, dict[str, Any]]:
    """Latest brain_docs per slice → {slice: {path, content, meta, body, version, updated_at}}."""
    rows = conn.execute(
        "SELECT path, slice, content, version, source, updated_at FROM brain_docs WHERE tenant_id = %s ORDER BY path",
        (tenant_id,),
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        meta, body = parse_front_matter(row["content"])
        out[row["slice"]] = {**row, "meta": meta, "body": body}
    return out


def main(argv: list[str] | None = None) -> int:
    from common.db import ensure_tenant, get_conn
    from common.settings import settings

    args = argv if argv is not None else sys.argv[1:]
    with get_conn() as conn:
        ensure_tenant(conn, settings.tenant_id)
        if args and args[0] == "export":
            written = export_repo(conn, settings.tenant_id, Path(args[1]) if len(args) > 1 else REPO_DIR)
            print(f"exported {len(written)} docs")
        else:
            versions = load_repo(conn, settings.tenant_id)
            print(f"loaded {len(versions)} docs: {versions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
