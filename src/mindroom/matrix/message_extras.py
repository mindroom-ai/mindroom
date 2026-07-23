"""MindRoom Matrix message extras content helpers."""

from __future__ import annotations

import collections.abc
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "MINDROOM_MESSAGE_EXTRAS_KEY",
    "MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS",
    "MINDROOM_MESSAGE_EXTRAS_MAX_SECTIONS",
    "MINDROOM_MESSAGE_EXTRAS_MAX_TITLE_CHARS",
    "MINDROOM_MESSAGE_EXTRAS_VERSION",
    "MessageExtraContentType",
    "MessageExtraSection",
    "build_message_extras_content",
    "parse_message_extra_sections",
]

MINDROOM_MESSAGE_EXTRAS_KEY = "com.mindroom.message_extras"
MINDROOM_MESSAGE_EXTRAS_VERSION = 2
MINDROOM_MESSAGE_EXTRAS_MAX_SECTIONS = 8
MINDROOM_MESSAGE_EXTRAS_MAX_TITLE_CHARS = 120
MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS = 16 * 1024

MessageExtraContentType = Literal["text/plain", "text/markdown", "text/html"]


@dataclass(frozen=True, slots=True)
class MessageExtraSection:
    """One collapsible MindRoom message extra section."""

    title: str
    content: str
    content_type: MessageExtraContentType = "text/markdown"
    collapsed: bool = True


def _parse_content_type(value: object) -> MessageExtraContentType:
    if not isinstance(value, str):
        msg = "message_extras sections require content_type to be a string."
        raise TypeError(msg)
    if value == "text/plain":
        return "text/plain"
    if value == "text/markdown":
        return "text/markdown"
    if value == "text/html":
        return "text/html"
    msg = "message_extras content_type must be one of text/plain, text/markdown, or text/html."
    raise ValueError(msg)


def parse_message_extra_sections(sections: Sequence[Mapping[str, object]]) -> list[MessageExtraSection]:
    """Parse model/tool supplied section dictionaries into validated sections."""
    if len(sections) > MINDROOM_MESSAGE_EXTRAS_MAX_SECTIONS:
        msg = f"message_extras supports at most {MINDROOM_MESSAGE_EXTRAS_MAX_SECTIONS} sections."
        raise ValueError(msg)

    parsed_sections: list[MessageExtraSection] = []
    for index, section in enumerate(sections, start=1):
        if not isinstance(section, collections.abc.Mapping):
            msg = f"message_extras section {index} must be an object."
            raise TypeError(msg)
        raw_title = section.get("title")
        if not isinstance(raw_title, str):
            msg = f"message_extras section {index} requires a string title."
            raise TypeError(msg)
        title = raw_title.strip()
        if not title:
            msg = f"message_extras section {index} requires a non-empty title."
            raise ValueError(msg)
        if len(title) > MINDROOM_MESSAGE_EXTRAS_MAX_TITLE_CHARS:
            msg = f"message_extras section {index} title cannot exceed {MINDROOM_MESSAGE_EXTRAS_MAX_TITLE_CHARS} characters."
            raise ValueError(msg)

        raw_content = section.get("content")
        if not isinstance(raw_content, str):
            msg = f"message_extras section {index} requires string content."
            raise TypeError(msg)
        if len(raw_content) > MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS:
            msg = (
                f"message_extras section {index} content cannot exceed "
                f"{MINDROOM_MESSAGE_EXTRAS_MAX_CONTENT_CHARS} characters."
            )
            raise ValueError(msg)

        content_type = _parse_content_type(section.get("content_type", "text/markdown"))
        collapsed = section.get("collapsed", True)
        if not isinstance(collapsed, bool):
            msg = f"message_extras section {index} requires a boolean for collapsed."
            raise TypeError(msg)
        parsed_sections.append(
            MessageExtraSection(
                title=title,
                content=raw_content,
                content_type=content_type,
                collapsed=collapsed,
            ),
        )

    return parsed_sections


def build_message_extras_content(sections: Sequence[MessageExtraSection]) -> dict[str, object]:
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
