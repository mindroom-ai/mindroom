"""The tool values the agent loop sees: ``ToolFunction``, the toolkits that group them, and the values tools return."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mindroom.llm.media import File, named_by_resource
from mindroom.llm.provider import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping

    from mindroom.llm.media import Audio, Image, Video
    from mindroom.llm.provider import MediaKind
    from mindroom.llm.runs import ToolExecution
    from mindroom.tool_system.invocation import ToolMiddleware

type ToolkitClose = Callable[[], Awaitable[None]]
type ToolkitConnect = Callable[[], Awaitable[ToolkitClose | None]]


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


# The gates whose call pauses its run until a human confirms or denies it.
CONFIRMATION_GATES = frozenset({ToolGate.CONFIRM, ToolGate.POLICY})
# The ``approval_type`` a paused call stores when an approval rule, not the tool, asks for its confirmation; a timed
# approval covers only such calls, and a tool's own confirmation is asked whatever the rules say.
POLICY_CONFIRMATION_APPROVAL_TYPE = "mindroom_policy"


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolFunction:
    """One callable tool with its model-facing definition and the flags the loop reads.

    ``parameters`` is the final JSON schema the model receives; readers must not mutate it.
    ``nullable_optional_parameters`` names derived parameters whose ``None`` branch ``parameters`` omitted because
    their default is ``None``, so execution-side validation still accepts explicit JSON ``null`` for them.
    ``compat_parameters`` names entrypoint parameters intentionally omitted from an open ``parameters`` schema for
    compactness or capability gating, so scheduled-call validation still accepts and type-checks them against the
    entrypoint without reopening parameters hidden because an action was disabled.
    ``description`` and ``strict`` are None when the tool never set them, which the wire omits.
    ``gate`` says why a call waits before it runs, None for a function whose calls run at once.
    ``stop_after_call`` stops the run after every call of the function, and ``may_stop_run`` marks a function whose
    call stops it only when the call's outcome says so (``ToolOutcome.stop_after_call``), as a model switch for the
    current response does; either way the function controls the run (``controls_run``).
    ``offer_background_after`` and ``offer_output_path`` control whether the model-facing schema projects the reserved
    ``background_after`` and ``mindroom_output_path`` parameters; runtime job execution and large-result auto-save stay
    active even when a function opts out of advertising them.
    ``middleware`` is the function's own policy, which every call of it runs innermost, inside whatever policy the
    caller runs, such as the computer announcement of the browser that gives an agent its worker computer.
    ``toolkit`` is the authored toolkit an agent is offered the function from, which approvals, scheduled calls, and
    tool jobs name it by; for an implied or preset tool that is the tool that brings it in.
    ``registered_toolkit`` is the registered tool the function comes from, which the agent CLI reaches it under,
    eager or deferred, and ``tool:environment`` hooks read; it differs from ``toolkit`` for an implied or preset tool.
    Both are None for a function generated for the agent, such as the knowledge search, the skill readers, and a
    team's delegation. The MCP gateway adapts the function a call names alone (``toolkit_function``) and sets only its
    ``registered_toolkit``, from the tool entry the call names; the gateway's search adapts a selected toolkit whole
    and sets neither. A script call's function carries both, set where the script policy checks its grant.
    """

    name: str
    toolkit: str | None = None
    registered_toolkit: str | None = None
    description: str | None
    parameters: Mapping[str, Any]
    nullable_optional_parameters: frozenset[str] = frozenset()
    compat_parameters: frozenset[str] = frozenset()
    strict: bool | None = None
    gate: ToolGate | None = None
    stop_after_call: bool = False
    may_stop_run: bool = False
    offer_background_after: bool = True
    offer_output_path: bool = True
    entrypoint: Callable[..., Any]
    middleware: tuple[ToolMiddleware, ...] = ()

    @property
    def controls_run(self) -> bool:
        """Whether a call of this function can stop the run, so it runs in the run that made it.

        Such a call runs inline, where its outcome reaches the run: never as a background job, a scheduled call, or a
        call through the standard agent CLI.
        """
        return self.stop_after_call or self.may_stop_run

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
        if self.gate is ToolGate.DELEGATION:
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
    ``connect`` is set for a toolkit that holds a connection for one run, which whoever offers its functions to a
    run connects before the run and closes after it; it returns how to close the connection, or None when the run
    owns none because the toolkit was connected before it or connecting failed.
    """

    name: str
    functions: tuple[ToolFunction, ...]
    instructions: str | None
    guidance: str | None = None
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
    fails it. ``counts_as_tool_failure`` says whether a failed outcome counts toward the agent loop's consecutive
    tool-failure circuit breaker (``False`` when the tool itself worked, such as a shell command exiting non-zero or
    timing out). ``refused_by`` is set only by the tool-call policy, for a call it refused before the body ran: a
    plugin hook declined it, or the run's approval gate denied it; a failed outcome a tool answers with leaves it
    unset.
    """

    stop_after_call: bool = False
    is_error: bool = False
    counts_as_tool_failure: bool = True
    refused_by: Literal["plugin_hook", "approval"] | None = None
    saved_output: dict[str, object] | None = None


def _normalize_file(file: File) -> File:
    """When inline bytes sit beside a path, keep the path's name and type and drop the path so readers never open it."""
    if file.content is not None and file.filepath is not None:
        return replace(named_by_resource(file, Path(file.filepath).name), filepath=None)
    return file


def read_tool_value(result: object) -> ToolValue | None:
    """Read a tool's return value as a ``ToolValue``; None for a value that is not a tool value."""
    if not isinstance(result, ToolValue):
        return None
    if any(file.content is not None and file.filepath is not None for file in result.files):
        return replace(result, files=tuple(_normalize_file(file) for file in result.files))
    return result


def media_reference(kind: MediaKind, medium: Image | Audio | Video | File) -> dict[str, object]:
    """Return a client's reference to one medium without its bytes: its kind, its MIME type, and its URL if any."""
    reference: dict[str, object] = {"kind": kind, "mime_type": medium.mime_type}
    if medium.url:
        reference["url"] = medium.url
    return reference
