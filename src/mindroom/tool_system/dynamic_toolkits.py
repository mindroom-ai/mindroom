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


def defer_toolkit(domain_name: str, description: str, toolkit: AdaptedToolkit) -> tuple[ToolDeferral, AdaptedToolkit]:
    """Return what tool search shows of a wire-deferred toolkit beside its functions, and the toolkit without them.

    The system prompt is sent before tool search finds a toolkit, so the instructions it and its functions would
    render travel with search results instead.
    """
    deferral = ToolDeferral(
        domain_name=domain_name,
        description=description,
        instructions=toolkit.instructions,
        function_instructions=toolkit.function_instructions,
    )
    return deferral, replace(toolkit, instructions=None, function_instructions={})
