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
    ``description``, ``strict``, ``requires_confirmation``, and ``external_execution`` keep Agno's tri-state: None
    means the tool never set the value, which the loop reads as False and the wire omits.
    ``external_execution_silent`` marks an externally executed call whose pause the run reports without text.
    ``injected_params`` names the entrypoint parameters the framework supplies at call time, which the model never
    sees and whose model-sent values are dropped.
    """

    name: str
    description: str | None
    parameters: Mapping[str, Any]
    strict: bool | None = None
    requires_confirmation: bool | None = None
    approval_type: str | None = None
    external_execution: bool | None = None
    external_execution_silent: bool | None = None
    stop_after_tool_call: bool = False
    show_result: bool = False
    entrypoint: Callable[..., Any]
    injected_params: frozenset[str] = frozenset()

    def definition(self) -> ToolSpec:
        """Return the model-facing definition a provider adapter formats: every field Agno serialized, None kept."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            strict=self.strict,
            requires_confirmation=self.requires_confirmation,
            external_execution=self.external_execution,
            approval_type=self.approval_type,
        )
