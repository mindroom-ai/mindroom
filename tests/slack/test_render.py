"""Tests for the plain dictionaries the Slack tools build from Slack answers."""

from __future__ import annotations

from typing import Any

import pytest

from mindroom.slack.render import slack_display_name


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
