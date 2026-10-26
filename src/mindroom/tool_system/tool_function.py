"""The tool values the agent loop sees: ``ToolFunction``, the toolkits that group them, and the values tools return.

They import no Agno module; ``agno_toolkits`` builds them from Agno's tool objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from mindroom.llm.provider import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping

    from mindroom.llm.media import Audio, File, Image, Video
    from mindroom.llm.provider import MediaKind
    from mindroom.llm.runs import ToolExecution
    from mindroom.tool_system.invocation import ToolMiddleware

type ToolkitClose = Callable[[], Awaitable[None]]
type ToolkitConnect = Callable[[], Awaitable[ToolkitClose | None]]


class InjectedValue(StrEnum):
    """What a framework-injected parameter receives at call time."""

    ACTOR = "actor"  # The adapted toolkit's agent or team stand-in, ``ToolCallActor``.
    RUN_CONTEXT = "run_context"
    MEDIA = "media"  # The run's media, which the owned loop never has, so None.
    DEFAULT = "default"  # Nothing, so the parameter keeps its default.


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFunction:
    """One callable tool with its model-facing definition and the flags the loop reads.

    ``parameters`` is the final JSON schema the model receives; readers must not mutate it.
    ``description``, ``strict``, ``requires_confirmation``, and ``external_execution`` are None when the tool never
    set them, which the loop reads as False and the wire omits.
    ``external_execution_silent`` marks an externally executed call whose pause the run reports without text.
    ``injected_params`` maps each entrypoint parameter the framework supplies at call time to what it receives; the
    model never sees one, and its model-sent values are dropped unless it is a media name the schema declares.
    ``entrypoint_processed`` marks a function that does not set ``skip_entrypoint_processing`` (MCP tools and a
    toolkit's ``@tool`` methods set it); realtime calls run Agno's entrypoint processing on such a function a second
    time, which closes its object schemas (see the TODO in ``matrix_rtc/call_tools.py``).
    ``actor_state`` is what an Agno toolkit keeps on its injected agent or team (``ToolCallActor``): one dict per
    adapted toolkit, shared by its functions and calls and never stored.
    ``middleware`` is the function's own policy, which every call of it runs innermost, inside whatever policy the
    caller runs, such as the computer announcement of the browser that gives an agent its worker computer.
    ``toolkit`` is the authored toolkit an agent is offered the function from, which approvals, scheduled calls, and
    tool jobs name it by; for an implied or preset tool that is the tool that brings it in.
    ``registered_toolkit`` is the registered tool the function comes from, which the agent CLI reaches it under,
    eager or deferred; it differs from ``toolkit`` for an implied or preset tool. Both are None for a
    function generated for the agent, such as the knowledge search, the skill readers, and a team's delegation, and
    for one adapted by a caller that names its toolkit beside it: the MCP gateway and the script broker adapt the
    function a call names alone (``adapt_function``), and the gateway's search adapts a selected toolkit whole.
    """

    name: str
    toolkit: str | None = None
    registered_toolkit: str | None = None
    description: str | None
    parameters: Mapping[str, Any]
    strict: bool | None = None
    requires_confirmation: bool | None = None
    approval_type: str | None = None
    external_execution: bool | None = None
    external_execution_silent: bool | None = None
    stop_after_call: bool = False
    entrypoint: Callable[..., Any]
    injected_params: Mapping[str, InjectedValue] = field(default_factory=dict)
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

    def planned_execution(self, call_id: str, arguments: dict[str, Any] | None) -> ToolExecution:
        """Return the execution a call of this function starts as, before it runs.

        The call is paused when the function requires confirmation or external execution, and a paused call carries
        the function's approval type and silence, as Agno's paused calls did.
        """
        # Deferred so that these tool values import no pydantic, which run records pull in through redaction.
        from mindroom.llm.runs import ToolExecution  # noqa: PLC0415

        execution = ToolExecution(tool_call_id=call_id, tool_name=self.name, tool_args=arguments)
        if self.requires_confirmation:
            execution.requires_confirmation = True
        if self.external_execution:
            execution.external_execution_required = True
        if execution.is_paused:
            execution.approval_type = self.approval_type
            execution.external_execution_silent = self.external_execution_silent
        return execution


@dataclass(frozen=True, slots=True)
class AdaptedToolkit:
    """One Agno toolkit as the agent loop sees it; ``instructions`` is set only when ``add_instructions`` asks for it.

    ``guidance`` is the toolkit's instructions whether or not the prompt shows them, which the agent CLI shows when
    it describes the toolkit's functions and minimal mode offers as a context document.
    ``function_instructions`` holds, by function name, the instructions a function asks the prompt to show, which
    only tool search shows, for a deferred toolkit; an agent refuses an eager toolkit that offers such a function.
    ``refused`` holds, by function name, why MindRoom refuses a function that sets a field the owned loop cannot
    honor, such as an Agno hook; an agent refuses a toolkit that still offers such a function.
    ``connect`` is set for a toolkit that holds a connection for one run, which whoever offers its functions to a
    run connects before the run and closes after it; it returns how to close the connection, or None when the run
    owns none because the toolkit was connected before it or connecting failed.
    """

    name: str
    functions: tuple[ToolFunction, ...]
    instructions: str | None
    guidance: str | None = None
    function_instructions: Mapping[str, str] = field(default_factory=dict)
    refused: Mapping[str, str] = field(default_factory=dict)
    connect: ToolkitConnect | None = None


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
    """A finished tool value: the text the model reads and the media the tool produced."""

    content: str
    images: tuple[Image, ...] = ()
    audios: tuple[Audio, ...] = ()
    videos: tuple[Video, ...] = ()
    files: tuple[File, ...] = ()

    def media(self) -> Iterator[tuple[MediaKind, Image | Audio | Video | File]]:
        """Yield each medium with its kind: the images, then the audio clips, videos, and files."""
        for kind, items in (
            ("image", self.images),
            ("audio", self.audios),
            ("video", self.videos),
            ("file", self.files),
        ):
            for item in items:
                yield kind, item


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolOutcome(ToolValue):
    """What one tool call produced: the text the model reads (the error text on failure) and the tool's media.

    A tool or middleware may answer a call with one: ``stop_after_call`` stops the run after it, and ``is_error``
    fails it, as the tool-call policy answers a call a plugin hook declined.
    """

    stop_after_call: bool = False
    is_error: bool = False


def media_reference(kind: MediaKind, medium: Image | Audio | Video | File) -> dict[str, object]:
    """Return a client's reference to one medium without its bytes: its kind, its MIME type, and its URL if any."""
    reference: dict[str, object] = {"kind": kind, "mime_type": medium.mime_type}
    if medium.url:
        reference["url"] = medium.url
    return reference
