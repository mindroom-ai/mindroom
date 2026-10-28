"""Tests for the plain dictionaries the Slack tools build from Slack answers."""

from __future__ import annotations

from typing import Any

import pytest

from mindroom.slack.render import slack_display_name, slack_plain_text


@pytest.mark.parametrize(
    ("user", "name"),
    [
        ({"id": "U0123ABCD", "name": "dana", "real_name": "Dana E", "profile": {"display_name": "Dana"}}, "Dana"),
        (
            {"id": "U0123ABCD", "name": "dana", "profile": {"display_name": "", "real_name": "Dana Example"}},
            "Dana Example",
        ),
        ({"id": "U0123ABCD", "name": "dana", "real_name": "Dana E", "profile": "not an object"}, "Dana E"),
        ({"id": "U0123ABCD", "name": "dana"}, "dana"),
        ({"id": "U0123ABCD", "name": 7}, "U0123ABCD"),
        ({}, ""),
    ],
)
def test_display_name_prefers_what_slack_shows(user: dict[str, Any], name: str) -> None:
    """The display name wins, then the full name, then the handle, then the user ID."""
    assert slack_display_name(user) == name


@pytest.mark.parametrize(
    ("text", "plain"),
    [
        ("<@U0123ABCD> and <@U0456EFGH|dana>, look", "@U0123ABCD and @U0456EFGH, look"),
        ("in <#C0123ABCD|launch-eng> and <#C0456EFGH>", "in #launch-eng and #C0456EFGH"),
        (
            "<https://example.com/a?b=1|the doc> or <https://example.com>",
            "the doc (https://example.com/a?b=1) or https://example.com",
        ),
        ("<!here> <!channel> <!subteam^S0123ABCD|@eng>", "@here @channel @eng"),
        ("a &lt;b&gt; &amp;lt; c", "a <b> &lt; c"),
        ("no markup", "no markup"),
    ],
)
def test_plain_text_renders_slack_markup_without_lookups(text: str, plain: str) -> None:
    """Mentions keep IDs, channels their sent names, links their label and URL, and Slack's escapes are undone."""
    assert slack_plain_text(text) == plain
