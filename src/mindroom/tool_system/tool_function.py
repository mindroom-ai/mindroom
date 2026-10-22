"""The tool values the owned loop sees: ``ToolFunction``, the toolkits that group them, and the values tools return.

They import no Agno module; ``agno_toolkits`` builds them from Agno's tool objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.provider import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.llm.media import Audio, File, Image, Video


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFunction:
    """One callable tool with its model-facing definition and the flags the loop reads.

    ``parameters`` is the final JSON schema the model receives; readers must not mutate it.
    ``description``, ``strict``, ``requires_confirmation``, and ``external_execution`` keep Agno's tri-state: None
    means the tool never set the value, which the loop reads as False and the wire omits.
    ``external_execution_silent`` marks an externally executed call whose pause the run reports without text.
    ``injected_params`` names the entrypoint parameters the framework supplies at call time, which the model never
    sees and whose model-sent values are dropped.
    ``entrypoint_processed`` marks a function whose entrypoint Agno would process again: one that does not set
    ``skip_entrypoint_processing``, as MCP tools and the ``@tool`` methods a toolkit registers do to keep the schema
    they were built with. Realtime calls repeat the second processing pass Agno ran on a marked function.
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
    entrypoint: Callable[..., Any]
    injected_params: frozenset[str] = frozenset()
    entrypoint_processed: bool = False

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


@dataclass(frozen=True, slots=True)
class AdaptedToolkit:
    """One Agno toolkit as the owned loop sees it; ``instructions`` is set only when Agno would add it to the prompt.

    ``guidance`` is the toolkit's instructions whether or not the prompt shows them, which the agent CLI shows when
    it describes the toolkit's functions and minimal mode offers as a context document.
    """

    name: str
    functions: tuple[ToolFunction, ...]
    instructions: str | None
    guidance: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolValue:
    """A finished tool value in owned terms: the text the model reads and the media the tool produced."""

    content: str
    images: tuple[Image, ...] = ()
    audios: tuple[Audio, ...] = ()
    videos: tuple[Video, ...] = ()
    files: tuple[File, ...] = ()
