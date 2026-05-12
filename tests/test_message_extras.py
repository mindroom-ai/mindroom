"""Tests for MindRoom Matrix message extras payloads."""

from __future__ import annotations

import pytest

from mindroom.matrix.message_extras import (
    MINDROOM_MESSAGE_EXTRAS_KEY,
    MessageExtraSection,
    build_message_extras_content,
    parse_message_extra_sections,
)


def test_build_message_extras_content_uses_cinny_schema() -> None:
    """Message extras should match the schema rendered by MindRoom Cinny."""
    content = build_message_extras_content(
        [
            MessageExtraSection(
                title="Evidence",
                content_type="text/html",
                content="<table><tr><td>42</td></tr></table>",
                collapsed=False,
            ),
            MessageExtraSection(
                title="Notes",
                content="**Markdown** details",
            ),
        ],
    )

    assert content == {
        MINDROOM_MESSAGE_EXTRAS_KEY: {
            "version": 2,
            "sections": [
                {
                    "title": "Evidence",
                    "content_type": "text/html",
                    "content": "<table><tr><td>42</td></tr></table>",
                    "collapsed": False,
                },
                {
                    "title": "Notes",
                    "content_type": "text/markdown",
                    "content": "**Markdown** details",
                    "collapsed": True,
                },
            ],
        },
    }


def test_parse_message_extra_sections_rejects_unsupported_content_type() -> None:
    """Tool inputs should fail before sending content Cinny will ignore."""
    with pytest.raises(ValueError, match="content_type"):
        parse_message_extra_sections(
            [
                {
                    "title": "Unsafe",
                    "content_type": "application/json",
                    "content": "{}",
                },
            ],
        )


@pytest.mark.parametrize(
    ("section", "match"),
    [
        ({"title": 42, "content": "details"}, "string title"),
        ({"title": "Details", "content": "details", "collapsed": "yes"}, "boolean"),
    ],
)
def test_parse_message_extra_sections_rejects_invalid_field_types(
    section: dict[str, object],
    match: str,
) -> None:
    """Malformed model inputs should fail with precise type errors."""
    with pytest.raises(TypeError, match=match):
        parse_message_extra_sections([section])
