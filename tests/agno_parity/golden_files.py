"""How every regenerable golden file is written, so that regenerating an unchanged golden reproduces it byte for byte.

A golden is pretty JSON in the key order its writer built; writers sort the keys whose order carries no meaning with
``sorted_keys`` and keep the order of values a provider or model receives as serialized bytes.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


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
