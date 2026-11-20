"""Other names of registered toolkits, accepted wherever a tool name is authored, stored, or asked for."""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator

TOOL_ALIASES: dict[str, str] = {"file": "filesystem", "coding": "filesystem"}


def canonical_tool_name(name: str) -> str:
    """Return ``name``, a toolkit or ``toolkit.function``, with an aliased toolkit spelled by its registered name."""
    toolkit, separator, function = name.partition(".")
    return TOOL_ALIASES.get(toolkit, toolkit) + separator + function


def canonical_tool_names[T: (list[str], tuple[str, ...])](names: T) -> T:
    """Return ``names`` canonical, each once, in their first order."""
    return type(names)(dict.fromkeys(canonical_tool_name(name) for name in names))


ToolNames = Annotated[list[str], AfterValidator(canonical_tool_names)]
