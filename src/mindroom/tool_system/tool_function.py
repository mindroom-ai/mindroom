"""The tool values the owned loop sees: ``ToolFunction``, the toolkits that group them, and the values tools return.

They import no Agno module; ``agno_toolkits`` builds them from Agno's tool objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.llm.provider import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping

    from mindroom.llm.media import Audio, File, Image, Video
    from mindroom.tool_system.invocation import ToolMiddleware

type ToolkitClose = Callable[[], Awaitable[None]]
type ToolkitConnect = Callable[[], Awaitable[ToolkitClose | None]]


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFunction:
    """One callable tool with its model-facing definition and the flags the loop reads.

    ``parameters`` is the final JSON schema the model receives; readers must not mutate it.
    ``description``, ``strict``, ``requires_confirmation``, and ``external_execution`` are None when the tool never
    set them, which the loop reads as False and the wire omits.
    ``external_execution_silent`` marks an externally executed call whose pause the run reports without text.
    ``injected_params`` names the entrypoint parameters the framework supplies at call time, which the model never
    sees and whose model-sent values are dropped.
    ``entrypoint_processed`` marks a function that does not set ``skip_entrypoint_processing`` (MCP tools and a
    toolkit's ``@tool`` methods set it); realtime calls run Agno's entrypoint processing on such a function a second
    time, which closes its object schemas (see the TODO in ``matrix_rtc/call_tools.py``).
    ``actor_state`` is what an Agno toolkit keeps on its injected agent or team (``ToolCallActor``): one dict per
    adapted toolkit, shared by its functions and calls and never stored.
    ``middleware`` is the function's own policy, which every call of it runs innermost, inside whatever policy the
    caller runs, such as the computer announcement of the browser that gives an agent its worker computer.
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
    actor_state: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    middleware: tuple[ToolMiddleware, ...] = ()

    # TODO(simplify): The definition carries the pause flags only because Chat and Responses send them on the wire,
    # as main does. Dropping them here and from ``ToolSpec`` leaves name, description, schema, and strict.
    # Models would notice: Chat and Responses requests stop sending them for the tools that set them (#3075).
    def definition(self) -> ToolSpec:
        """Return the model-facing definition a provider adapter formats, with the flags the tool never set as None."""
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
    """One Agno toolkit as the owned loop sees it; ``instructions`` is set only when ``add_instructions`` asks for it.

    ``guidance`` is the toolkit's instructions whether or not the prompt shows them, which the agent CLI shows when
    it describes the toolkit's functions and minimal mode offers as a context document.
    ``connect`` is set for a toolkit that holds a connection for one run, which whoever offers its functions to a
    run connects before the run and closes after it; it returns how to close the connection, or None when the run
    owns none because the toolkit was connected before it or connecting failed.
    """

    name: str
    functions: tuple[ToolFunction, ...]
    instructions: str | None
    guidance: str | None = None
    connect: ToolkitConnect | None = None


def first_of_each_name[T](
    pairs: Iterable[tuple[T, ToolFunction]],
    on_dropped: Callable[[T, ToolFunction], None] | None = None,
) -> Iterator[tuple[T, ToolFunction]]:
    """Yield the pairs whose function is the first of its name, in order, the selection a run offers.

    Each later pair of a taken name is dropped, kept as main behaves, and passed to ``on_dropped``.
    """
    names: set[str] = set()
    for source, function in pairs:
        if function.name in names:
            if on_dropped is not None:
                on_dropped(source, function)
            continue
        names.add(function.name)
        yield source, function


async def connect_toolkits(toolkits: Iterable[AdaptedToolkit], closes: list[ToolkitClose]) -> None:
    """Connect each of ``toolkits`` that holds a connection for one run, adding how to close it to ``closes``.

    ``closes`` grows as each toolkit connects, so a caller cancelled partway still closes the ones it connected.
    """
    for toolkit in toolkits:
        if toolkit.connect is None:
            continue
        close = await toolkit.connect()
        if close is not None:
            closes.append(close)


async def close_toolkits(closes: list[ToolkitClose]) -> None:
    """Close what ``connect_toolkits`` connected, the last one first, emptying ``closes``."""
    while closes:
        await closes.pop()()


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolValue:
    """A finished tool value in owned terms: the text the model reads and the media the tool produced."""

    content: str
    images: tuple[Image, ...] = ()
    audios: tuple[Audio, ...] = ()
    videos: tuple[Video, ...] = ()
    files: tuple[File, ...] = ()
