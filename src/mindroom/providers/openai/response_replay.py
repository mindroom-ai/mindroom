"""Replay a stored assistant message on a Responses request: from its checkpoint, its recorded output, or a rebuild.

Every Responses reply keeps its response's ordered output items under ``RESPONSE_OUTPUT_KEY`` in its
``provider_data``, which is part of the stored run format. A reply whose output carries encrypted reasoning goes as
recorded, so the reasoning stays attached to the items it produced; any other message, another provider's included,
is rebuilt from its canonical text and calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.native_compaction import checkpoint_items
from mindroom.providers.openai.legacy_response_replay import legacy_tool_search_items

if TYPE_CHECKING:
    from mindroom.llm.messages import Message

__all__ = ["RESPONSE_OUTPUT_KEY", "assistant_replay_items", "recorded_output"]

RESPONSE_OUTPUT_KEY = "mindroom_response_output"
_TOOL_SEARCH_ITEM_TYPES = frozenset({"tool_search_call", "tool_search_output"})


def recorded_output(message: Message) -> list[dict[str, Any]] | None:
    """Return the ordered output items the response behind ``message`` recorded, or None when it recorded none."""
    items = (message.provider_data or {}).get(RESPONSE_OUTPUT_KEY)
    return items if isinstance(items, list) else None


def assistant_replay_items(message: Message, native_route: str | None) -> list[dict[str, Any]]:
    """Return the input items that replay one assistant message, whose calls carry the request's tool-call ids.

    A checkpoint on ``native_route`` stands for the message, and a recorded output carrying encrypted reasoning goes
    as recorded while canonical history still holds what it records; any other message is rebuilt.
    """
    if checkpoint := checkpoint_items(message, native_route):
        return checkpoint
    calls = _function_call_items(message)
    output = recorded_output(message)
    if output is None:
        return _rebuilt(message, legacy_tool_search_items(message), calls)
    if (replayed := _as_recorded(message, output, calls)) is not None:
        return replayed
    return _rebuilt(message, [item for item in output if item.get("type") in _TOOL_SEARCH_ITEM_TYPES], calls)


def _function_call_items(message: Message) -> list[dict[str, Any]]:
    return [
        {
            "type": "function_call",
            "id": call.get("id"),
            "call_id": call.get("call_id", call.get("id")),
            "name": call["function"]["name"],
            "arguments": call["function"]["arguments"],
            "status": "completed",
        }
        for call in message.tool_calls or ()
    ]


def _as_recorded(
    message: Message,
    output: list[dict[str, Any]],
    calls: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Return ``output`` with the turn's canonical calls in place of the recorded ones, or None when it cannot go.

    It goes only when all its reasoning carries encrypted content: output without reasoning holds nothing a rebuild
    loses, and a reasoning item without encrypted content, as a stored response returns it, cannot be resent. Output
    whose text differs from the canonical text, or that lacks one of the canonical calls, no longer matches the turn,
    so the turn is rebuilt. A recorded call canonical history no longer holds stays out.
    """
    reasoning = [item for item in output if item.get("type") == "reasoning"]
    if not reasoning or not all(item.get("encrypted_content") for item in reasoning):
        return None
    text = "".join(
        part.get("text", "")
        for item in output
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    if text != (message.content or ""):
        return None
    by_call_id = {call["call_id"]: call for call in calls}
    if not by_call_id.keys() <= {item.get("call_id") for item in output if item.get("type") == "function_call"}:
        return None
    return [
        by_call_id[item.get("call_id")] if item.get("type") == "function_call" else item
        for item in output
        if item.get("type") != "function_call" or item.get("call_id") in by_call_id
    ]


def _rebuilt(
    message: Message,
    search_items: list[dict[str, Any]],
    calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild a message from its canonical text and calls, after the hosted tool searches its response ran.

    The rebuild sends no reasoning, so no item keeps its provider item ID: the provider rejects a function call's or a
    hosted search item's ID sent without the reasoning item that preceded it, stored or not.
    """
    content = message.content
    if isinstance(content, list):
        content = [
            {"type": "input_text", "text": part}
            if isinstance(part, str)
            else {"type": "input_text", "text": part["text"]}
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
            else part
            for part in content
        ]
    if message.tool_calls:
        # A turn's text goes ahead of its calls.
        text = [{"role": "assistant", "content": content}] if content else []
    else:
        text = [{"role": "assistant", "content": content if content is not None else ""}]
    return [{key: value for key, value in item.items() if key != "id"} for item in (*search_items, *text, *calls)]
