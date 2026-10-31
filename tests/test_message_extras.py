"""Tests for MindRoom Matrix message extras payloads."""

from __future__ import annotations

import pytest

from mindroom.matrix.message_extras import (
    MINDROOM_MESSAGE_EXTRAS_KEY,
    MatrixMessageExtra,
    build_message_extras_content,
    parse_message_extra_sections,
)


def test_build_message_extras_content_uses_cinny_schema() -> None:
    """Message extras should match the schema rendered by MindRoom Chat."""
    content = build_message_extras_content(
        [
            MatrixMessageExtra(
                title="Evidence",
                content_type="text/html",
                content="<table><tr><td>42</td></tr></table>",
                collapsed=False,
            ),
            MatrixMessageExtra(
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


@pytest.mark.parametrize(
    ("sections", "match"),
    [
        ([MatrixMessageExtra(title="  ", content="details")], "section 1 requires a non-empty title"),
        ([MatrixMessageExtra(title="t" * 121, content="details")], "title cannot exceed 120 characters"),
        (
            [MatrixMessageExtra(title="Details", content="x" * (16 * 1024 + 1))],
            "content cannot exceed 16384 characters",
        ),
        ([MatrixMessageExtra(title="Details", content="details")] * 9, "at most 8 sections"),
    ],
)
def test_parse_message_extra_sections_refuses_sections_over_the_limits(
    sections: list[MatrixMessageExtra],
    match: str,
) -> None:
    """The limits Chat renders within are refused before anything is sent."""
    with pytest.raises(ValueError, match=match):
        parse_message_extra_sections(sections)


def test_parse_message_extra_sections_strips_titles_and_keeps_section_settings() -> None:
    """A section keeps its content, type, and collapsed flag; only its title is stripped."""
    [section] = parse_message_extra_sections(
        [MatrixMessageExtra(title="  Evidence ", content=" raw ", content_type="text/html", collapsed=False)],
    )

    assert section == MatrixMessageExtra(title="Evidence", content=" raw ", content_type="text/html", collapsed=False)
