# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Rewrite stored tool-call ids outside an OpenAI wire's id rule, such as another provider's, into its shape.

A thread can switch models mid-conversation, so a Chat request may replay Claude ``toolu_`` ids and a Responses
request may replay Chat ``call_`` ids. Foreign ids map to deterministic counters, so the same history always produces
the same request bytes. A Chat turn stores each call under an id that already fits the Chat rule (``chat_call_id``), so
its own calls reach the model under their stored ids.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.messages import Message

__all__ = ["chat_call_id", "reformat_tool_call_ids"]


@dataclass(frozen=True, slots=True)
class _IdRule:
    prefix: str
    max_length: int | None
    call_id_prefix: str | None


_RULES: dict[str, _IdRule] = {
    "chat": _IdRule(prefix="call_", max_length=40, call_id_prefix=None),
    "responses": _IdRule(prefix="fc_", max_length=None, call_id_prefix="call_"),
}


def _fits(call_id: str, rule: _IdRule) -> bool:
    return call_id.startswith(rule.prefix) and not (rule.max_length and len(call_id) > rule.max_length)


def chat_call_id(server_id: object) -> str:
    """Return ``server_id`` when it fits the Chat rule, else a new id that does, for a Chat turn to store its call under.

    Chat requests rewrite every id outside the rule, so a call stored under the server's id would reach the model under
    another one, and an approval receipt naming the stored id would name no call the model sees. The new id is
    ``call_`` and a uuid4's 32 hex digits, which fits the rule's 40 characters.
    """
    if isinstance(server_id, str) and _fits(server_id, _RULES["chat"]):
        return server_id
    return f"call_{uuid4().hex}"


def _foreign_ids(messages: Sequence[Message], rule: _IdRule) -> tuple[dict[str, str], dict[str, str]]:
    """Return new ids for each foreign call id (and its call id), plus the Responses ``call_`` ids."""
    id_map: dict[str, str] = {}
    call_id_map: dict[str, str] = {}
    counter = 0
    for call in (call for message in messages if message.role == "assistant" for call in message.tool_calls or ()):
        old_id = call.get("id")
        if not old_id or old_id in id_map:
            continue
        if _fits(old_id, rule):
            continue
        digits = rule.max_length - len(rule.prefix) if rule.max_length else 8
        id_map[old_id] = f"{rule.prefix}{counter:0{digits}x}"
        existing_call_id = call.get("call_id")
        if existing_call_id and isinstance(existing_call_id, str) and existing_call_id != old_id:
            id_map[existing_call_id] = id_map[old_id]
        if rule.call_id_prefix:
            call_id_map[old_id] = (
                existing_call_id
                if isinstance(existing_call_id, str) and existing_call_id.startswith(rule.call_id_prefix)
                else f"{rule.call_id_prefix}{counter:08x}"
            )
        counter += 1
    return id_map, call_id_map


def reformat_tool_call_ids(messages: Sequence[Message], wire: Literal["chat", "responses"]) -> list[Message]:
    """Return copies whose foreign tool-call ids, and their results' ids, match the wire's id rule.

    Responses calls also get a matching ``call_`` id, keeping an existing ``call_`` id when present.
    Messages that need no change are returned as the same objects.
    """
    rule = _RULES[wire]
    id_map, call_id_map = _foreign_ids(messages, rule)
    if not id_map:
        return list(messages)
    result: list[Message] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            calls = copy.deepcopy(message.tool_calls)
            for call in calls:
                old_id = call.get("id")
                if old_id and old_id in id_map:
                    call["id"] = id_map[old_id]
                    if rule.call_id_prefix:
                        call["call_id"] = call_id_map.get(old_id, id_map[old_id])
            result.append(replace(message, tool_calls=calls))
        elif message.role == "tool" and message.tool_call_id and message.tool_call_id in id_map:
            result.append(replace(message, tool_call_id=id_map[message.tool_call_id]))
        else:
            result.append(message)
    return result
