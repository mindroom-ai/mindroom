"""Enrichment rendering helpers."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .types import EnrichmentItem

_TRANSIENT_CONTEXT_OPEN = "<mindroom_transient_context>"
_TRANSIENT_CONTEXT_CLOSE = "</mindroom_transient_context>"


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
    return "<mindroom_message_context>\n" + "\n".join(rendered_items) + "\n</mindroom_message_context>"


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
