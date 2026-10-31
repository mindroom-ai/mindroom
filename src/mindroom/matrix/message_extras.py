"""MindRoom Matrix message extras content helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "MINDROOM_MESSAGE_EXTRAS_KEY",
    "MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS",
    "MINDROOM_MESSAGE_EXTRAS_MAX_SECTIONS",
    "MINDROOM_MESSAGE_EXTRAS_MAX_TITLE_CHARS",
    "MINDROOM_MESSAGE_EXTRAS_VERSION",
    "MatrixMessageExtra",
    "MessageExtraContentType",
    "build_message_extras_content",
    "parse_message_extra_sections",
]

MINDROOM_MESSAGE_EXTRAS_KEY = "com.mindroom.message_extras"
MINDROOM_MESSAGE_EXTRAS_VERSION = 2
MINDROOM_MESSAGE_EXTRAS_MAX_SECTIONS = 8
MINDROOM_MESSAGE_EXTRAS_MAX_TITLE_CHARS = 120
MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS = 16 * 1024

MessageExtraContentType = Literal["text/plain", "text/markdown", "text/html"]


class MatrixMessageExtra(BaseModel):
    """Model-facing fields for one optional collapsible section."""

    model_config = ConfigDict(strict=True, extra="forbid")

    title: str
    content: str
    content_type: MessageExtraContentType = "text/markdown"
    collapsed: bool = True


def parse_message_extra_sections(sections: Sequence[MatrixMessageExtra]) -> list[MatrixMessageExtra]:
    """Return the sections a tool was given, titles stripped, refusing ones over the limits or without a title."""
    if len(sections) > MINDROOM_MESSAGE_EXTRAS_MAX_SECTIONS:
        msg = f"message_extras supports at most {MINDROOM_MESSAGE_EXTRAS_MAX_SECTIONS} sections."
        raise ValueError(msg)

    parsed_sections: list[MatrixMessageExtra] = []
    for index, section in enumerate(sections, start=1):
        title = section.title.strip()
        if not title:
            msg = f"message_extras section {index} requires a non-empty title."
            raise ValueError(msg)
        if len(title) > MINDROOM_MESSAGE_EXTRAS_MAX_TITLE_CHARS:
            msg = f"message_extras section {index} title cannot exceed {MINDROOM_MESSAGE_EXTRAS_MAX_TITLE_CHARS} characters."
            raise ValueError(msg)
        if len(section.content) > MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS:
            msg = (
                f"message_extras section {index} content cannot exceed "
                f"{MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS} characters."
            )
            raise ValueError(msg)
        parsed_sections.append(section.model_copy(update={"title": title}))

    return parsed_sections


def build_message_extras_content(sections: Sequence[MatrixMessageExtra]) -> dict[str, object]:
    """Build Matrix event content for MindRoom-aware clients to render as extras."""
    return {
        MINDROOM_MESSAGE_EXTRAS_KEY: {
            "version": MINDROOM_MESSAGE_EXTRAS_VERSION,
            "sections": [
                {
                    "title": section.title,
                    "content_type": section.content_type,
                    "content": section.content,
                    "collapsed": section.collapsed,
                }
                for section in sections
            ],
        },
    }
