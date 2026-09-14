"""Capture and replay ordered OpenAI output at the Agno provider boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.native_compaction import checkpoint_items

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from agno.models.message import Message
    from agno.models.response import ModelResponse

_TOOL_SEARCH_ITEMS_KEY = "tool_search_items"
_TOOL_SEARCH_ITEM_TYPES = frozenset({"tool_search_call", "tool_search_output"})
_RESPONSE_OUTPUT_KEY = "mindroom_response_output"


def record_tool_search_items(model_response: ModelResponse, output_items: Iterable[Any]) -> None:
    """Store tool_search output items on one response's provider data.

    Agno's Responses parser only handles message/function_call/reasoning
    items, so the search items would otherwise be dropped and could never be
    replayed. Both the non-streaming output list and streamed
    ``response.output_item.done`` items land here; Agno's provider-data merge
    extends lists, so streamed items accumulate in arrival order.
    """
    items = [item.model_dump(exclude_none=True) for item in output_items if item.type in _TOOL_SEARCH_ITEM_TYPES]
    if not items:
        return
    if model_response.provider_data is None:
        model_response.provider_data = {}
    model_response.provider_data.setdefault(_TOOL_SEARCH_ITEMS_KEY, []).extend(items)


def record_response_output(model_response: ModelResponse, items: list[dict[str, Any]]) -> None:
    """Retain reasoning in its original position beside text, search, and function calls.

    Agno stores only the last reasoning item and omits it when replaying tool
    calls. Capture complete output only when that lossy conversion matters.
    Checkpoints have a separate route-bound owner; this is canonical replay.
    """
    if any(item.get("type") == "reasoning" for item in items):
        model_response.provider_data = {
            **(model_response.provider_data or {}),
            _RESPONSE_OUTPUT_KEY: [item for item in items if item.get("type") != "compaction"],
        }


def formatted_input_with_provider_items(
    messages: Sequence[Message],
    formatted_input: list[Any],
    *,
    native_route: str | None,
    replay_reasoning: bool,
) -> list[Any]:
    """Replace each replayed assistant span once, preserving canonical tool edits.

    One cursor owns checkpoint, reasoning, and hosted-search insertion. Agno
    still owns message conversion, media, call-ID mapping, and tool results.
    Callers supply only the messages after any stored-response continuation.
    """
    prepared_input = list(formatted_input)
    cursor = 0
    for message in messages:
        if message.role != "assistant":
            continue
        anchor = _anchor_index(prepared_input, cursor, message)
        if anchor is None:
            continue
        data = message.provider_data or {}
        # Agno emits calls, or one text item followed by its single reasoning item.
        size = len(message.tool_calls) if message.tool_calls else 1
        if not message.tool_calls and replay_reasoning and data.get("reasoning_output") is not None:
            size += 1
        replacement = checkpoint_items(message, native_route) or _canonical_response_output(
            message,
            prepared_input[anchor : anchor + size],
        )
        if replacement is not None:
            prepared_input[anchor : anchor + size] = replacement
            cursor = anchor + len(replacement)
        else:
            items = data.get(_TOOL_SEARCH_ITEMS_KEY) or []
            prepared_input[anchor:anchor] = items
            cursor = anchor + len(items) + size
    return prepared_input


def _canonical_response_output(message: Message, formatted_span: list[Any]) -> list[dict[str, Any]] | None:
    """Replay original ordering using only the text and calls still in canonical history."""
    items = (message.provider_data or {}).get(_RESPONSE_OUTPUT_KEY)
    if not isinstance(items, list):
        return None
    text = "".join(
        part.get("text", "")
        for item in items
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    if text != (message.content or ""):
        return None
    calls = {
        item.get("call_id"): item
        for item in formatted_span
        if isinstance(item, dict) and item.get("type") == "function_call"
    }
    captured_call_ids = {item.get("call_id") for item in items if item.get("type") == "function_call"}
    if not calls.keys() <= captured_call_ids:
        return None
    return [
        calls[item.get("call_id")] if item.get("type") == "function_call" else item
        for item in items
        if item.get("type") != "function_call" or item.get("call_id") in calls
    ]


def _anchor_index(formatted_input: list[Any], start: int, message: Message) -> int | None:
    """Return the formatted-input index where one message's items belong."""
    if message.tool_calls:
        anchor_ids = {tool_call.get("id") for tool_call in message.tool_calls}
        anchor_ids |= {tool_call.get("call_id") for tool_call in message.tool_calls}
        anchor_ids.discard(None)
        for index in range(start, len(formatted_input)):
            item = formatted_input[index]
            if (
                isinstance(item, dict)
                and item.get("type") == "function_call"
                and (item.get("id") in anchor_ids or item.get("call_id") in anchor_ids)
            ):
                return index
        return None
    content = message.content if message.content is not None else ""
    for index in range(start, len(formatted_input)):
        item = formatted_input[index]
        if isinstance(item, dict) and item.get("role") == "assistant" and item.get("content") == content:
            return index
    return None
