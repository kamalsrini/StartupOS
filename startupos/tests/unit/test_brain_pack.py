"""Context pack assembly: section order, token cap, truncation priority; front-matter parsing; repo seeds."""

from datetime import UTC, datetime

from brain import pack
from brain.sync import REPO_DIR, SLICES, parse_front_matter, read_repo, render_front_matter

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def sections(**over):
    base = dict.fromkeys(pack.SECTION_ORDER, "body")
    base.update(over)
    return base


def test_sections_appear_in_contract_order():
    content = pack.assemble(sections(), "unitone", NOW)
    positions = [content.index(f"\n## {pack.TITLES[name]}\n") for name in pack.SECTION_ORDER]
    assert positions == sorted(positions)
    assert content.startswith("# Context pack · unitone · 2026-09-04\n")
    assert pack.estimate_tokens(content) <= pack.MAX_TOKENS


def test_pack_is_capped_and_truncates_lowest_priority_first():
    big = "x" * 30_000  # 7.5k tokens each
    content = pack.assemble(sections(gtm=big, build=big, identity="short identity"), "unitone", NOW)
    assert pack.estimate_tokens(content) <= pack.MAX_TOKENS
    assert "short identity" in content
    # gtm has lower priority than build, so it is cut first — and cut harder.
    gtm_part = content.split("\n## GTM state\n")[1].split("\n## Open signals\n")[0]
    build_part = content.split("\n## Build state\n")[1].split("\n## GTM state\n")[0]
    assert pack.TRUNC_MARK.strip() in gtm_part
    assert len(gtm_part) < len(build_part)


def test_pack_truncates_everything_when_all_sections_are_huge():
    big = "y" * 50_000
    content = pack.assemble(sections(**dict.fromkeys(pack.SECTION_ORDER, big)), "unitone", NOW)
    assert pack.estimate_tokens(content) <= pack.MAX_TOKENS
    for name in pack.SECTION_ORDER:
        assert f"\n## {pack.TITLES[name]}\n" in content  # every section keeps its heading


def test_assemble_is_deterministic():
    a = pack.assemble(sections(gtm="z" * 45_000), "unitone", NOW)
    b = pack.assemble(sections(gtm="z" * 45_000), "unitone", NOW)
    assert a == b


def test_estimate_tokens_is_chars_over_four():
    assert pack.estimate_tokens("") == 0
    assert pack.estimate_tokens("abcd") == 1
    assert pack.estimate_tokens("abcde") == 2


def test_front_matter_roundtrip():
    meta, body = parse_front_matter(
        '---\nslice: gtm\nupdated: 2026-09-04\ncampaigns: [{"id": "a", "targets": 18}]\n---\n# GTM\n\nbody\n'
    )
    assert meta == {"slice": "gtm", "updated": "2026-09-04", "campaigns": [{"id": "a", "targets": 18}]}
    assert body == "# GTM\n\nbody"
    again, _ = parse_front_matter(render_front_matter(meta, body))
    assert again == meta


def test_front_matter_absent_or_unterminated():
    assert parse_front_matter("plain text") == ({}, "plain text")
    assert parse_front_matter("---\nslice: x\nno end") == ({}, "---\nslice: x\nno end")


def test_repo_seed_files_cover_every_slice_with_front_matter():
    docs = read_repo(REPO_DIR)
    slices = {d["slice"] for d in docs}
    assert slices == set(SLICES)
    for d in docs:
        assert {"slice", "updated", "source"} <= set(d["meta"]), d["path"]
        assert d["body"].strip(), d["path"]
    gtm = next(d for d in docs if d["slice"] == "gtm")
    campaigns = gtm["meta"]["campaigns"]
    assert campaigns[0]["status"] == "draft" and campaigns[0]["since"] == "2026-06-03" and campaigns[0]["targets"] == 18
    assert sum(s["contacts"] for s in gtm["meta"]["sequences"]) == 42 + 36 + 763


def test_no_llm_imports_in_tier0_dirs():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    offenders = []
    for d in ("brain", "ingest", "signals"):
        for f in (root / d).rglob("*.py"):
            tree = ast.parse(f.read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for n in names:
                    if n.split(".")[0] == "anthropic":
                        offenders.append(f"{f}: {n}")
                    if n.split(".")[0] == "daemon" and not (isinstance(node, ast.ImportFrom) and node.col_offset > 0):
                        offenders.append(f"{f}: top-level daemon import {n}")
    assert offenders == []
