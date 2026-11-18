"""The agent-authored marker counts by its presence, in an event's content or an edit's replacement content."""

from __future__ import annotations

import pytest

from mindroom.constants import AGENT_AUTHORED_KEY
from mindroom.matrix.agent_authored import carries_agent_authored_marker


@pytest.mark.parametrize("value", ["@mind:example.com", "", True, {}, None])
def test_the_marker_counts_whatever_its_value(value: object) -> None:
    """Only the key's presence matters, so no value can make a marked event count as its sender's own."""
    assert carries_agent_authored_marker({"body": "hi", AGENT_AUTHORED_KEY: value})


def test_the_marker_counts_inside_an_edits_new_content() -> None:
    """An edit marked only in its replacement content is still marked."""
    assert carries_agent_authored_marker(
        {"body": "* hi", "m.new_content": {"body": "hi", AGENT_AUTHORED_KEY: "@mind:example.com"}},
    )


@pytest.mark.parametrize(
    "content",
    [{}, {"body": "hi"}, {"m.new_content": "not a mapping"}, {"m.new_content": {"body": "hi"}}],
)
def test_content_without_the_marker_does_not_count(content: dict[str, object]) -> None:
    """Content without the key, in either layer, is not marked."""
    assert not carries_agent_authored_marker(content)
