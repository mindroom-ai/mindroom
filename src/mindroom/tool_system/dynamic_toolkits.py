"""What tool search shows of a deferred toolkit, and the functions a run offers of it."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

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


# Where a deferred toolkit's instructions reach the model, which depends on what its tool search shows up front.
type DeferredInstructions = Literal["search_results", "descriptions", "prompt"]


def defer_toolkit(
    domain_name: str,
    description: str,
    toolkit: AdaptedToolkit,
    *,
    instructions: DeferredInstructions,
) -> tuple[ToolDeferral, AdaptedToolkit]:
    """Return what tool search shows of a wire-deferred toolkit beside its functions, and the toolkit without them.

    The instructions the toolkit and its functions would render go where its tool search shows them:

    - ``search_results``: portable tool search returns them beside the definitions it finds.
    - ``descriptions``: native search that shows nothing of a function until a search loads it gets them in each
      function's description.
    - ``prompt``: native search that shows every function's name and description up front keeps the toolkit's in
      the system prompt, once, and puts each function's own in its description.
    """
    deferral = ToolDeferral(
        domain_name=domain_name,
        description=description,
        instructions=toolkit.instructions,
        function_instructions=toolkit.function_instructions,
    )
    if instructions == "search_results":
        return deferral, replace(toolkit, instructions=None, function_instructions={})
    shared = toolkit.instructions if instructions == "descriptions" else None
    functions = tuple(
        replace(
            function,
            description=_joined(function.description, shared, toolkit.function_instructions.get(function.name)),
        )
        for function in toolkit.functions
    )
    kept = toolkit.instructions if instructions == "prompt" else None
    return deferral, replace(toolkit, functions=functions, instructions=kept, function_instructions={})


def _joined(*parts: str | None) -> str | None:
    """Return the non-empty parts joined by blank lines, or None when every part is empty."""
    return "\n\n".join(part.strip() for part in parts if part and part.strip()) or None
