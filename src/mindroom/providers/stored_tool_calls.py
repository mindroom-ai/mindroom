"""How every wire reads the tool calls stored in a thread's history.

A thread can switch models mid-conversation, so a request may replay calls another provider stored: a Chat call
the output limit cut off mid-arguments, a Gemini call stored with empty arguments, or a shape an earlier release
wrote. Every wire formats history from ``repair_legacy_tool_calls`` copies, so each stored call names a function
and carries arguments text, and a wire that sends arguments as an object reads them with ``call_arguments``; no
stored shape fails one route's request before it is sent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.llm.call_tool import arguments_object

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["call_arguments"]


def call_arguments(function: Mapping[str, Any]) -> dict[str, Any]:
    """Return a stored call's arguments as an object, ``{}`` when they are missing or not a JSON object."""
    return arguments_object(function.get("arguments")) or {}
