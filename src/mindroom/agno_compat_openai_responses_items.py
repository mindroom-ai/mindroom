"""Capture provider output types omitted by Agno's Responses parser."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agno.models.response import ModelResponse

TOOL_SEARCH_ITEMS_KEY = "tool_search_items"
RESPONSE_OUTPUT_KEY = "mindroom_response_output"
_TOOL_SEARCH_ITEM_TYPES = frozenset({"tool_search_call", "tool_search_output"})

# AGNO_COMPAT: Responses parsing loses reasoning items needed for replay.
# Reason: Agno 3.0.9 persists only the last reasoning item and can omit reasoning
# when stored Responses output must later be replayed explicitly.
# Upstream issue: https://github.com/agno-agi/agno/issues/9960
# Upstream PR: https://github.com/agno-agi/agno/pull/9968, closed in favour of
# https://github.com/agno-agi/agno/pull/10075 (released in Agno 3.0.11) and
# https://github.com/agno-agi/agno/pull/10395 (merged, unreleased after 3.0.11);
# their coverage of every reasoning item is unverified.
# Remove when: The pinned Agno release preserves complete ordered reasoning beside
# stored text and function calls during explicit replay.
# Coverage: tests/test_openai_native_compaction.py::test_reasoning_survives_native_tool_loop;
# tests/test_openai_native_compaction.py::test_ordered_replay_respects_canonical_tool_filtering.

# AGNO_COMPAT: Responses parsing omits hosted tool-search items.
# Reason: Agno 3.0.9 ignores hosted tool-search call and output items, so a later
# explicit replay loses that provider output.
# Upstream issue: No matching issue identified; hosted-search output capture is untracked.
# Upstream PR: None identified.
# Remove when: A pinned Agno release parses and replays hosted-search output in
# provider order, or exposes a stable output-item extension hook.
# Coverage: tests/test_openai_native_compaction.py::test_reasoning_survives_native_tool_loop.


def record_tool_search_items(model_response: ModelResponse, output_items: Iterable[Any]) -> None:
    """Append provider-only search items in arrival order."""
    items = [item.model_dump(exclude_none=True) for item in output_items if item.type in _TOOL_SEARCH_ITEM_TYPES]
    if not items:
        return
    if model_response.provider_data is None:
        model_response.provider_data = {}
    model_response.provider_data.setdefault(TOOL_SEARCH_ITEMS_KEY, []).extend(items)


def record_response_output(model_response: ModelResponse, items: list[dict[str, Any]]) -> None:
    """Retain complete ordered output only when reasoning would otherwise be lost."""
    if any(item.get("type") == "reasoning" for item in items):
        model_response.provider_data = {
            **(model_response.provider_data or {}),
            RESPONSE_OUTPUT_KEY: [item for item in items if item.get("type") != "compaction"],
        }
