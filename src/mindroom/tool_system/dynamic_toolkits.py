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
    # Instructions the toolkit and its functions would render when eager, which portable tool search returns.
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
    """Return what tool search shows of a wire-deferred toolkit beside its functions, and the toolkit to offer.

    Portable tool search returns the instructions the toolkit and its functions would render beside the definitions
    it finds, so the toolkit offered carries none.
    Native tool search expands only function definitions, so the toolkit keeps its instructions for the system
    prompt and each function's own instructions go in its description.
    """
    deferral = ToolDeferral(
        domain_name=domain_name,
        description=description,
        instructions=toolkit.instructions if portable_search else None,
        function_instructions=toolkit.function_instructions if portable_search else {},
    )
    if portable_search:
        return deferral, replace(toolkit, instructions=None, function_instructions={})
    functions = tuple(
        _with_own_instructions(function, toolkit.function_instructions[function.name])
        if function.name in toolkit.function_instructions
        else function
        for function in toolkit.functions
    )
    return deferral, replace(toolkit, functions=functions, function_instructions={})


def _with_own_instructions(function: ToolFunction, instructions: str) -> ToolFunction:
    """Return ``function`` with the instructions it asks the prompt to show after its description."""
    description = f"{function.description}\n\n{instructions}" if function.description else instructions
    return replace(function, description=description)
