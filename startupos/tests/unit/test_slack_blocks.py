"""Track D — the pure Block Kit renderers. No I/O: every test here is a function call and an assertion.

What matters to Slack (and to the founder reading a notification on a phone): every block has a `type`, no
block's text exceeds 3000 characters, `fallback_text` is never empty, and the approval buttons carry the
`action_id`/`value` pair Track B dispatches on.
"""

from __future__ import annotations

import pytest

from daemon import slack_blocks as sb

LONG = "x" * 7000
UNICODE = "🚀 明日のプルス — café naïve · Ünicode ✅"


def every_block_is_valid(fallback: str, blocks: list[dict]) -> None:
    """The invariant every renderer must satisfy."""
    assert isinstance(fallback, str) and fallback.strip(), "fallback_text must be non-empty"
    assert len(fallback) <= sb.MAX_FALLBACK
    assert isinstance(blocks, list) and blocks
    for b in blocks:
        assert b.get("type"), f"block without type: {b}"
    assert sb.validate(blocks) == []


ALL_RENDERS = {
    "pulse": lambda: sb.pulse_blocks("line one\nline two", [{"label": "open", "val": "3"}], 2, "https://app/x"),
    "digest": lambda: sb.digest_blocks({"text": "done\nwaiting", "date": "2026-09-13", "approvals_pending": 1}),
    "signal": lambda: sb.signal_blocks({"id": "s1", "module": "build", "severity": "high", "title": "UNI-158"}),
    "approval": lambda: sb.approval_blocks(
        {"id": "a1", "type": "Linear · assign", "target": "UNI-158", "preview": "p"}
    ),
    "approval_decided": lambda: sb.approval_blocks({"id": "a1", "type": "t", "target": "UNI-158"}, decided="approved"),
}


@pytest.mark.parametrize("name", sorted(ALL_RENDERS))
def test_every_renderer_returns_valid_blocks_and_a_fallback(name):
    every_block_is_valid(*ALL_RENDERS[name]())


@pytest.mark.parametrize("name", sorted(ALL_RENDERS))
def test_no_block_exceeds_slack_limits_with_pathological_input(name):
    """Same renderers, every text field replaced with 7000 characters."""
    renders = {
        "pulse": lambda: sb.pulse_blocks(LONG, [{"label": LONG, "val": LONG}], 999999, LONG),
        "digest": lambda: sb.digest_blocks({"text": LONG, "date": LONG, "approvals_pending": LONG}),
        "signal": lambda: sb.signal_blocks(
            {"id": LONG, "module": LONG, "severity": LONG, "title": LONG, "meta": LONG, "suggested_skill": LONG},
            overflow=12,
        ),
        "approval": lambda: sb.approval_blocks({"id": LONG, "type": LONG, "target": LONG, "preview": LONG}),
        "approval_decided": lambda: sb.approval_blocks({"id": LONG, "type": LONG}, decided={"status": LONG}),
    }
    every_block_is_valid(*renders[name]())


# --- truncation / splitting ---------------------------------------------------------------------------


def test_truncate_never_exceeds_the_limit_and_marks_the_cut():
    assert sb.truncate("short", 10) == "short"
    out = sb.truncate("y" * 100, 10)
    assert len(out) == 10 and out.endswith("…")
    assert sb.truncate(None, 10) == ""


def test_long_pulse_text_is_split_across_sections_not_truncated():
    text = "\n".join(f"line {i} " + "w" * 80 for i in range(100))  # ~8.7k chars
    fallback, blocks = sb.pulse_blocks(text, [], 0, "https://app")
    every_block_is_valid(fallback, blocks)
    sections = [b for b in blocks if b["type"] == "section"]
    assert len(sections) >= 3, "an 8k-character pulse must become several sections"
    joined = "".join(s["text"]["text"] for s in sections)
    assert "line 0" in joined and "line 99" in joined, "no content lost inside the limit"


def test_absurdly_long_text_truncates_rather_than_blowing_the_block_cap():
    fallback, blocks = sb.pulse_blocks("z" * 400_000, [], 0, None)
    every_block_is_valid(fallback, blocks)
    assert len(blocks) <= sb.MAX_BLOCKS
    assert blocks[-2]["text"]["text"].endswith("…") or blocks[-1]["elements"][0]["text"].endswith("…")


def test_split_text_prefers_line_boundaries():
    parts = sb.split_text("aaaa\nbbbb\ncccc", limit=6)
    assert parts == ["aaaa", "bbbb", "cccc"]


def test_finish_caps_blocks_at_fifty_with_a_marker():
    out = sb.finish([{"type": "divider"}] * 80)
    assert len(out) == sb.MAX_BLOCKS and out[-1]["type"] == "context"


# --- unicode ------------------------------------------------------------------------------------------


def test_unicode_survives_and_is_counted_in_characters():
    fallback, blocks = sb.pulse_blocks(UNICODE, [{"label": "café", "val": "3"}], 1, "https://app")
    every_block_is_valid(fallback, blocks)
    assert UNICODE in blocks[1]["text"]["text"]
    assert fallback == UNICODE
    # a limit counted in characters, not bytes: 10 emoji is 10 characters, not 40
    assert len(sb.truncate("🚀" * 100, 10)) == 10


# --- pulse --------------------------------------------------------------------------------------------


def test_pulse_shows_tiles_the_approval_count_and_the_web_link():
    fallback, blocks = sb.pulse_blocks("four lines", [{"label": "open issues", "val": "31"}], 2, "https://app/cockpit")
    assert blocks[0]["type"] == "header" and "Morning pulse" in blocks[0]["text"]["text"]
    trail = " ".join(e["text"] for b in blocks if b["type"] == "context" for e in b["elements"])
    assert "*31* open issues" in trail
    assert "2 approvals waiting" in trail
    assert "<https://app/cockpit|Open the cockpit>" in trail
    assert fallback == "four lines"


def test_pulse_singular_approval_and_empty_text_still_notify():
    fallback, blocks = sb.pulse_blocks("", None, 1, None)
    every_block_is_valid(fallback, blocks)
    assert "1 approval waiting" in fallback
    trail = " ".join(e["text"] for b in blocks if b["type"] == "context" for e in b["elements"])
    assert "Open the cockpit" in trail and "<" not in trail  # no URL → plain label, never a broken link


# --- digest -------------------------------------------------------------------------------------------


def test_digest_accepts_a_plain_string_or_a_dict():
    f1, b1 = sb.digest_blocks("what happened today")
    every_block_is_valid(f1, b1)
    assert f1 == "what happened today"
    f2, b2 = sb.digest_blocks({"text": "x", "date": "2026-09-13", "approvals_pending": 3, "open_signals": 7})
    every_block_is_valid(f2, b2)
    assert "2026-09-13" in b2[0]["text"]["text"]
    trail = " ".join(e["text"] for b in b2 if b["type"] == "context" for e in b["elements"])
    assert "3 approvals waiting" in trail and "7 signals open" in trail


def test_digest_with_nothing_at_all_is_still_a_valid_message():
    every_block_is_valid(*sb.digest_blocks({}))


# --- signal -------------------------------------------------------------------------------------------


def test_signal_renders_severity_link_and_trail():
    signal = {
        "id": "build.unassigned_high:UNI-158",
        "module": "build",
        "severity": "high",
        "title": "UNI-158 is High and unassigned",
        "meta": "3 days",
        "href": "https://linear.app/x",
        "suggested_skill": "build.assign_owner",
    }
    fallback, blocks = sb.signal_blocks(signal)
    every_block_is_valid(fallback, blocks)
    body = blocks[0]["text"]["text"]
    assert sb.SEVERITY_EMOJI["high"] in body
    assert "<https://linear.app/x|UNI-158 is High and unassigned>" in body
    assert "3 days" in body
    trail = blocks[-1]["elements"][0]["text"]
    assert "build · high" in trail and "build.assign_owner" in trail
    assert fallback == "[high] build: UNI-158 is High and unassigned"


def test_signal_overflow_line_is_appended_only_when_asked():
    _, plain = sb.signal_blocks({"id": "s", "title": "t"}, overflow=0)
    _, over = sb.signal_blocks({"id": "s", "title": "t"}, overflow=4)
    assert "more waiting" not in plain[-1]["elements"][0]["text"]
    assert "+4 more waiting in the cockpit" in over[-1]["elements"][0]["text"]


def test_signal_without_href_or_meta_still_renders():
    every_block_is_valid(*sb.signal_blocks({"id": "s1", "title": "bare"}))


# --- approval -----------------------------------------------------------------------------------------


APPROVAL = {
    "id": "assign-uni-158",
    "tenant_id": "unitone",
    "module": "build",
    "type": "Linear · assign",
    "target": "UNI-158",
    "preview": "Assign UNI-158 to Alexey",
}


def _buttons(blocks):
    return [el for b in blocks if b["type"] == "actions" for el in b["elements"]]


def test_approval_renders_approve_and_decline_with_action_id_and_value():
    fallback, blocks = sb.approval_blocks(APPROVAL)
    every_block_is_valid(fallback, blocks)
    buttons = _buttons(blocks)
    assert [b["action_id"] for b in buttons] == [sb.APPROVE_ACTION, sb.DECLINE_ACTION]
    assert {b["value"] for b in buttons} == {"assign-uni-158"}
    assert [b["style"] for b in buttons] == ["primary", "danger"]
    assert "Approval needed" in fallback or "Linear · assign" in fallback


def test_approval_accepts_a_pydantic_model():
    from common.models import Approval, Exec

    model = Approval(
        id="a9",
        tenant_id="unitone",
        module="build",
        type="Slack · nudge",
        target="UNI-1",
        preview="nudge",
        exec=Exec(server="Slack", tool="post_message", input={"channel": "C1", "text": "hi"}),
    )
    fallback, blocks = sb.approval_blocks(model)
    every_block_is_valid(fallback, blocks)
    assert _buttons(blocks)[0]["value"] == "a9"


def test_decided_approval_has_no_buttons_so_it_cannot_be_pressed_twice():
    fallback, blocks = sb.approval_blocks(APPROVAL, decided={"status": "approved", "decided_by": "u-kamal"})
    every_block_is_valid(fallback, blocks)
    assert _buttons(blocks) == []
    trail = blocks[-1]["elements"][0]["text"]
    assert "Approved" in trail and "u-kamal" in trail


def test_decided_declined_shows_the_reason_and_failure_shows_the_error():
    _, declined = sb.approval_blocks(APPROVAL, decided={"status": "declined", "decline_reason": "not now"})
    assert "not now" in declined[-1]["elements"][0]["text"]
    _, failed = sb.approval_blocks(APPROVAL, decided={"status": "failed", "result": {"error": "SlackError: boom"}})
    assert "execution failed: SlackError: boom" in failed[-1]["elements"][0]["text"]
    _, done = sb.approval_blocks(APPROVAL, decided={"status": "executed", "result": {"text": "Updated UNI-158"}})
    assert "Updated UNI-158" in done[-1]["elements"][0]["text"]


# --- validate -----------------------------------------------------------------------------------------


def test_validate_catches_what_slack_would_reject():
    assert sb.validate([]) == ["blocks must be a non-empty list"]
    assert "missing type" in sb.validate([{"text": "no type"}])[0]
    too_long = [{"type": "section", "text": {"type": "mrkdwn", "text": "x" * 3001}}]
    assert "3001 chars > 3000" in sb.validate(too_long)[0]
    no_action = [{"type": "actions", "elements": [{"type": "button", "value": "v"}]}]
    assert "button without action_id" in sb.validate(no_action)[0]
    no_value = [{"type": "actions", "elements": [{"type": "button", "action_id": "a"}]}]
    assert "button without value" in sb.validate(no_value)[0]
    assert sb.validate([{"type": "divider"}] * 51)[0].endswith("blocks > 50")
