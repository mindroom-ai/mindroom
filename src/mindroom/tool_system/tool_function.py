"""The tool values the agent loop sees: ``ToolFunction``, the toolkits that group them, and the values tools return.

They import no Agno module; ``agno_toolkits`` builds them from Agno's tool objects and from owned toolkits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

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
    MEDIA = "media"  # The run's media, which the agent loop never gives a tool, so None.
    DEFAULT = "default"  # Nothing, so the parameter keeps its default.


class ToolGate(StrEnum):
    """Why a call of a function waits before it runs; a function without a gate runs each call at once."""

    # The tool asks a human to confirm each call, whatever the approval rules say; its run pauses for the answer.
    CONFIRM = "confirm"
    # An approval rule asks a human to approve the call; its run pauses for the answer.
    POLICY = "policy"
    # An approval rule asks a human as the first phase of the background job the call runs as; its run goes on.
    JOB = "job"
    # The delegation driver runs the call's child agent outside the loop; its run pauses until the child answers.
    DELEGATION = "delegation"
    # The tool's author runs the call outside MindRoom, as an Agno function may ask; nothing in MindRoom answers it,
    # so a run that pauses on it fails.
    EXTERNAL = "external"


# The gates whose call pauses its run until a human confirms or denies it.
CONFIRMATION_GATES = frozenset({ToolGate.CONFIRM, ToolGate.POLICY})
# The gates whose call runs outside the loop, which pauses its run until the call's result comes back.
EXTERNAL_GATES = frozenset({ToolGate.DELEGATION, ToolGate.EXTERNAL})
# The ``approval_type`` a paused call stores when an approval rule, not the tool, asks for its confirmation; a timed
# approval covers only such calls, and a tool's own confirmation is asked whatever the rules say.
POLICY_CONFIRMATION_APPROVAL_TYPE = "mindroom_policy"


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFunction:
    """One callable tool with its model-facing definition and the flags the loop reads.

    ``parameters`` is the final JSON schema the model receives; readers must not mutate it.
    ``description`` and ``strict`` are None when the tool never set them, which the wire omits.
    ``gate`` says why a call waits before it runs, None for a function whose calls run at once.
    ``injected_params`` maps each entrypoint parameter the framework supplies at call time to what it receives; the
    model never sees one, and its model-sent values are dropped unless it is a media name the schema declares.
    ``actor_state`` is what an Agno toolkit keeps on its injected agent or team (``ToolCallActor``): one dict per
    adapted toolkit, shared by its functions and calls and never stored.
    ``middleware`` is the function's own policy, which every call of it runs innermost, inside whatever policy the
    caller runs, such as the computer announcement of the browser that gives an agent its worker computer.
    ``toolkit`` is the authored toolkit an agent is offered the function from, which approvals, scheduled calls, and
    tool jobs name it by; for an implied or preset tool that is the tool that brings it in.
    ``registered_toolkit`` is the registered tool the function comes from, which the agent CLI reaches it under,
    eager or deferred, and ``tool:environment`` hooks read; it differs from ``toolkit`` for an implied or preset tool.
    Both are None for a function generated for the agent, such as the knowledge search, the skill readers, and a
    team's delegation. The MCP gateway and the script broker adapt the function a call names alone
    (``toolkit_function``) and set only its ``registered_toolkit``, from the tool entry the call names; the gateway's
    search adapts a selected toolkit whole and sets neither.
    """

    name: str
    toolkit: str | None = None
    registered_toolkit: str | None = None
    description: str | None
    parameters: Mapping[str, Any]
    strict: bool | None = None
    gate: ToolGate | None = None
    stop_after_call: bool = False
    entrypoint: Callable[..., Any]
    injected_params: Mapping[str, InjectedValue] = field(default_factory=dict)
    actor_state: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    middleware: tuple[ToolMiddleware, ...] = ()

    def definition(self) -> ToolSpec:
        """Return the model-facing definition a provider adapter formats; a field the tool never set is None."""
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters, strict=self.strict)

    def planned_execution(self, call_id: str, arguments: dict[str, Any] | None) -> ToolExecution:
        """Return the execution a call of this function starts as, before it runs.

        A call whose gate pauses its run starts paused. A confirmation an approval rule asks for stores
        ``POLICY_CONFIRMATION_APPROVAL_TYPE``, which decides how it is approved.
        """
        # Deferred so that these tool values import no pydantic, which run records pull in through redaction.
        from mindroom.llm.runs import ToolExecution  # noqa: PLC0415

        execution = ToolExecution(tool_call_id=call_id, tool_name=self.name, tool_args=arguments)
        if self.gate in CONFIRMATION_GATES:
            execution.requires_confirmation = True
        if self.gate in EXTERNAL_GATES:
            execution.external_execution_required = True
        if self.gate is ToolGate.POLICY:
            execution.approval_type = POLICY_CONFIRMATION_APPROVAL_TYPE
        return execution


@dataclass(frozen=True, slots=True)
class AdaptedToolkit:
    """One toolkit as the agent loop sees it; ``instructions`` is what the prompt shows of its instructions.

    The prompt shows them whenever the toolkit offers a function, unless the toolkit is deferred behind portable tool
    search, which shows them instead. ``guidance`` is the toolkit's instructions whether or not the prompt shows them,
    which the agent CLI shows when it describes the toolkit's functions and minimal mode offers as a context document.
    ``function_instructions`` holds, by function name, the instructions a function asks the prompt to show, which a
    deferred toolkit hands to tool search or the function's description; an agent refuses an eager toolkit that
    offers such a function.
    ``refused`` holds, by function name, why MindRoom refuses a function that sets a field the agent loop cannot
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
    fails it. ``refused_by`` is set only by the tool-call policy, for a call it refused before the body ran: a plugin
    hook declined it, or the run's approval gate denied it; a failed outcome a tool answers with leaves it unset.
    """

    stop_after_call: bool = False
    is_error: bool = False
    refused_by: Literal["plugin_hook", "approval"] | None = None


def media_reference(kind: MediaKind, medium: Image | Audio | Video | File) -> dict[str, object]:
    """Return a client's reference to one medium without its bytes: its kind, its MIME type, and its URL if any."""
    reference: dict[str, object] = {"kind": kind, "mime_type": medium.mime_type}
    if medium.url:
        reference["url"] = medium.url
    return reference
