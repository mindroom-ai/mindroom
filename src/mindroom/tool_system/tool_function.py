"""The one tool shape the owned loop sees: a finished schema, the run-loop flags, and the callable that runs it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.provider import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFunction:
    """One callable tool with its model-facing definition and the flags the loop reads.

    ``parameters`` is the final JSON schema the model receives; readers must not mutate it.
    ``injected_params`` names the entrypoint parameters the framework supplies at call time, which the model never
    sees and whose model-sent values are dropped.
    """

    name: str
    description: str | None
    parameters: Mapping[str, Any]
    strict: bool = False
    requires_confirmation: bool = False
    approval_type: str | None = None
    external_execution: bool = False
    stop_after_tool_call: bool = False
    show_result: bool = False
    entrypoint: Callable[..., Any]
    injected_params: frozenset[str] = frozenset()

    def definition(self) -> ToolSpec:
        """Return the model-facing definition a provider adapter formats."""
        return ToolSpec(
            name=self.name,
            description=self.description or "",
            parameters=self.parameters,
            strict=self.strict,
        )
