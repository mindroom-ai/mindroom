"""Enrichment rendering helpers."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .types import EnrichmentItem

_TRANSIENT_CONTEXT_OPEN = "<mindroom_transient_context>"
_TRANSIENT_CONTEXT_CLOSE = "</mindroom_transient_context>"
_MESSAGE_CONTEXT_OPEN = "<mindroom_message_context>"
_MESSAGE_CONTEXT_CLOSE = "</mindroom_message_context>"


def _render_items(items: Sequence[EnrichmentItem]) -> list[str]:
    return [
        "\n".join(
            (
                (
                    f'<item key="{html.escape(item.key, quote=True)}" '
                    f'cache_policy="{html.escape(item.cache_policy, quote=True)}">'
                ),
                html.escape(item.text),
                "</item>",
            ),
        )
        for item in items
    ]


def render_enrichment_block(items: list[EnrichmentItem]) -> str:
    """Render enrichment items into one model-facing XML-like block."""
    if not items:
        return ""
    rendered_items = _render_items(items)
    return f"{_MESSAGE_CONTEXT_OPEN}\n" + "\n".join(rendered_items) + f"\n{_MESSAGE_CONTEXT_CLOSE}"


def _after_block(text: str) -> str:
    """Drop the spaces and blank lines that follow a block, keeping the indentation of the next line."""
    body = text.lstrip()
    if not body:
        return ""
    gap = text[: len(text) - len(body)]
    last_newline = gap.rfind("\n")
    return text[last_newline + 1 :] if last_newline >= 0 else body


def strip_enrichment_blocks(text: str) -> str:
    """Remove every block ``render_enrichment_block`` wrote, with the blank lines around it.

    Text with no block comes back unchanged. Where blocks sat between two pieces of text, one blank line joins them.
    This scans the text once, because it reads messages of any size, including long pasted whitespace.
    """
    pieces: list[str] = []
    start = 0
    while (opening := text.find(_MESSAGE_CONTEXT_OPEN, start)) >= 0:
        closing = text.find(_MESSAGE_CONTEXT_CLOSE, opening + len(_MESSAGE_CONTEXT_OPEN))
        if closing < 0:
            break
        pieces.append(text[start:opening])
        start = closing + len(_MESSAGE_CONTEXT_CLOSE)
    if not pieces:
        return text
    pieces.append(text[start:])
    pieces[0] = pieces[0].rstrip()
    pieces[1:] = [_after_block(piece) for piece in pieces[1:]]
    pieces[1:-1] = [piece.rstrip() for piece in pieces[1:-1]]
    return "\n\n".join(piece for piece in pieces if piece)


def render_transient_context(parts: Sequence[str]) -> str:
    """Wrap non-persisted current-turn context in one recognizable block."""
    body = "\n\n".join(part for part in parts if part)
    if not body:
        return ""
    return f"{_TRANSIENT_CONTEXT_OPEN}\n{body}\n{_TRANSIENT_CONTEXT_CLOSE}"


def is_transient_context(text: object) -> bool:
    """Return whether text is a block produced by ``render_transient_context``."""
    return (
        isinstance(text, str)
        and text.startswith(f"{_TRANSIENT_CONTEXT_OPEN}\n")
        and text.endswith(f"\n{_TRANSIENT_CONTEXT_CLOSE}")
    )


def render_system_enrichment_block(items: Sequence[EnrichmentItem]) -> str:
    """Render system enrichment items with deterministic cache-aware ordering."""
    if not items:
        return ""

    stable_items = sorted((item for item in items if item.cache_policy == "stable"), key=lambda item: item.key)
    volatile_items = sorted((item for item in items if item.cache_policy == "volatile"), key=lambda item: item.key)
    ordered_items = stable_items + volatile_items
    rendered_items = _render_items(ordered_items)
    return "<mindroom_system_context>\n" + "\n".join(rendered_items) + "\n</mindroom_system_context>"
