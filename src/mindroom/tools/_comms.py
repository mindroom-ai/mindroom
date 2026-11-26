"""Narrow configurable messaging dispatch schemas to enabled actions."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.tool_system.tool_function import ToolFunction


def restrict_actions(
    function: ToolFunction,
    allowed: tuple[str, ...],
    fields: dict[str, str] | None = None,
    *,
    common: tuple[str, ...] = (),
) -> ToolFunction:
    """Keep allowed action bullets (``- `action`: ...``) and their parameter properties."""
    properties = dict(function.parameters["properties"])
    properties["action"] = {**properties["action"], "enum": list(allowed)}
    if fields is not None:
        keep = {"action", *common, *(field for action in allowed for field in fields[action].split())}
        properties = {name: value for name, value in properties.items() if name in keep}
    description = "\n".join(
        line
        for line in (function.description or "").splitlines()
        if not line.startswith("- `") or any(f"`{action}`" in line.partition(":")[0] for action in allowed)
    )
    return replace(function, description=description, parameters={**function.parameters, "properties": properties})
