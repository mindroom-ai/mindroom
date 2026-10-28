"""Responses replies that releases before ordered output on every response stored, read for replay."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mindroom.llm.messages import Message

__all__ = ["legacy_tool_search_items"]


# LEGACY_COMPAT: Responses replies stored with their hosted tool-search items apart from an ordered output.
# Legacy format: an assistant message whose ``provider_data`` holds no ``mindroom_response_output`` list and keeps the
# hosted tool-search items of its response under ``tool_search_items``. Releases from v2026.7.58 recorded every
# response's search items there, and those from v2026.9.129 also the ordered output, but only of a completed unstored
# or portable response that reasoned, so a stored reply, an unstored one without reasoning, and one that did not
# complete have this shape.
# Last legacy release: v2026.10.647; replacement: the first release after it records the ordered output of every
# response that ends, search items included, and no ``tool_search_items``.
# Handling: the reply is rebuilt from its canonical text and calls after these search items, without reasoning or item
# ids, as that release sent it. ``tool_search_items`` beside an ordered output is ignored, since the output holds the
# same items.
# Coverage: tests/providers/test_openai_family_requests.py::test_a_reply_v2026_10_647_stored_replays_as_that_release_sent_it
def legacy_tool_search_items(message: Message) -> list[dict[str, Any]]:
    """Return the hosted tool-search items a reply stored without an ordered output kept apart."""
    return (message.provider_data or {}).get("tool_search_items") or []
