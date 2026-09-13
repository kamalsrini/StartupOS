"""`common.tenants.normalize_slack_channel` — the one place a founder's channel string is judged.

Pure, no DB: the API calls it before writing `tenants.slack_channel`, so a typo is a 422 in Settings rather than
a `channel_not_found` the daemon discovers at 7am.
"""

from __future__ import annotations

import pytest

from common.tenants import BadChannel, normalize_slack_channel


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("#ops", "#ops"),
        ("  #ops  ", "#ops"),
        ("#Ops-Room", "#ops-room"),  # Slack channel names are lower-case
        ("#a", "#a"),
        ("#team_1.x", "#team_1.x"),
        ("#" + "x" * 79, "#" + "x" * 79),
        ("C0123ABCD", "C0123ABCD"),  # a channel id keeps its case
        ("G01AB2CD3EF", "G01AB2CD3EF"),
        ("D0123ABCD", "D0123ABCD"),
    ],
)
def test_valid_channels_round_trip(given, expected):
    assert normalize_slack_channel(given) == expected


@pytest.mark.parametrize("given", [None, "", "   "])
def test_empty_means_unset(given):
    """Unset is a real answer: "post wherever the Slack install put StartupOS"."""
    assert normalize_slack_channel(given) is None


@pytest.mark.parametrize(
    "given",
    [
        "ops",  # no '#': ambiguous with an id
        "#with space",
        "#-",  # must start with a letter or digit
        "##ops",
        "#ops!",
        "#" + "x" * 90,  # Slack's limit is 80
        "C0",  # too short to be an id
        "c0123abcd",  # ids are upper-case
        "X0123ABCD",  # not a channel/group/DM id
        "kv:slack_bot_token",
        "https://slack.com/#ops",
    ],
)
def test_anything_else_is_refused(given):
    with pytest.raises(BadChannel):
        normalize_slack_channel(given)
