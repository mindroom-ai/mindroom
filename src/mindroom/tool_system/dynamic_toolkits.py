"""What tool search shows of a deferred toolkit, and the functions a run offers of it."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.tool_system.tool_function import AdaptedToolkit, ToolFunction


@dataclass(frozen=True)
class ToolDeferral:
    """What tool search shows of one authored deferred tool beside its functions."""

    domain_name: str
    description: str
    # Instructions the toolkit and its functions would render when eager, which the prompt leaves out.
    instructions: str | None
    function_instructions: Mapping[str, str]


@dataclass(frozen=True)
class DeferredToolkit:
    """The functions a run offers of one authored deferred tool, which stay off the provider request until searched."""

    deferral: ToolDeferral
    functions: tuple[ToolFunction, ...]

    @property
    def wire_function_names(self) -> tuple[str, ...]:
        """Return the names of the functions a run offers of the tool."""
        return tuple(function.name for function in self.functions)


def defer_toolkit(
    domain_name: str,
    description: str,
    toolkit: AdaptedToolkit,
    *,
    portable_search: bool,
) -> tuple[ToolDeferral, AdaptedToolkit]:
    """Return what tool search shows of a wire-deferred toolkit beside its functions, and the toolkit without them.

    The system prompt is sent before tool search finds a toolkit, so the instructions it and its functions would
    render travel with search results instead.
    Portable tool search returns them beside the definitions it finds.
    Native tool search expands only the function definitions, so there each function's description carries them.
    """
    deferral = ToolDeferral(
        domain_name=domain_name,
        description=description,
        instructions=toolkit.instructions,
        function_instructions=toolkit.function_instructions,
    )
    functions = toolkit.functions
    if not portable_search:
        functions = tuple(
            replace(function, description=_with_instructions(function, toolkit)) for function in toolkit.functions
        )
    return deferral, replace(toolkit, functions=functions, instructions=None, function_instructions={})


def _with_instructions(function: ToolFunction, toolkit: AdaptedToolkit) -> str | None:
    """Return the function's description followed by the instructions its toolkit and the function would render."""
    parts = (function.description, toolkit.instructions, toolkit.function_instructions.get(function.name))
    return "\n\n".join(part.strip() for part in parts if part and part.strip()) or None
