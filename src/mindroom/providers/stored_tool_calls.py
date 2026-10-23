"""How a wire that sends tool-call arguments as an object reads the arguments of a stored call.

A thread can switch models mid-conversation, so a request may replay calls another provider stored: a Chat call
the output limit cut off mid-arguments, or a Gemini call stored with empty arguments. Stored history reaches the
wires with every call holding a ``function`` with its ``arguments`` (``history/legacy_stored_tool_calls.py`` reads
the shapes earlier releases stored), but those arguments are whatever the storing provider produced: JSON text,
possibly cut off or empty, or an object.
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
