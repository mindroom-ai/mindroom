"""How every regenerable golden file is written, so that regenerating an unchanged golden reproduces it byte for byte.

A golden is pretty JSON in the key order its writer built; writers sort the keys whose order carries no meaning with
``sorted_keys`` and keep the order of values a provider or model receives as serialized bytes. ``turn_message`` is
the one encoding of a request or reply message that the provider and prompt goldens share.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.messages import Message

# What ``Message.to_dict()`` stores beside the turn itself, none of which a provider adapter sends or parses: the
# message's storage identity, and the flags saying it was replayed from history and stopped its run's tool loop.
STORAGE_ONLY_MESSAGE_KEYS = frozenset({"id", "created_at", "from_history", "stop_after_tool_call"})


def turn_message(message: Message) -> dict[str, Any]:
    """Return ``message`` as the goldens record a provider-neutral turn.

    That is its stored fields without the storage-only ones, in sorted order, each value as stored.
    """
    stored = message.to_dict()
    return {key: stored[key] for key in sorted(stored) if key not in STORAGE_ONLY_MESSAGE_KEYS}


def render(value: object) -> str:
    """Return the text of a golden file holding ``value``, in the key order ``value`` has."""
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def sorted_keys(value: Any) -> Any:  # noqa: ANN401 - arbitrary JSON
    """Return ``value`` with the keys of every nested object in sorted order."""
    if isinstance(value, dict):
        return {key: sorted_keys(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [sorted_keys(item) for item in value]
    return value


def write(path: Path, value: object) -> bool:
    """Write the golden file holding ``value`` at ``path`` and return whether its text changed."""
    text = render(value)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True
