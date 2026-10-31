# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Agent runs and approval resumes over the model loop.

A run assembles its messages, drives the loop, and ends in exactly one terminal record. Its stream ends with
``RunFinished`` carrying a ``COMPLETED``, ``PAUSED``, or ``ERROR`` record, and after an ``ERROR`` record raises the
exception that ended the run; a cancelled run stores a ``CANCELLED`` record and its cancellation propagates. Each
terminal record is handed to the persist callback once, before the run yields or raises its ending, so a caller that
writes its own record for the same run afterwards always writes last.

``drive_run``, ``resumed_record``, and ``decide_requirements`` are the pieces an agent run and a resume are made of;
the team runtime drives a team leader's record through the same pieces.
"""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol, TypedDict

from mindroom.agent_loop.model_loop import NO_HOOKS, LoopEnded, LoopHooks, run_model_loop
from mindroom.agent_loop.tools import PreparedTools
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.failure_text import failure_text
from mindroom.llm.events import RunFinished
from mindroom.llm.messages import is_own_model_message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import RunMetrics
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.tool_function import close_toolkits, connect_toolkits

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from mindroom.agent_loop.spec import AgentSpec, RunContext
    from mindroom.llm.events import RunEvent
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider, ModelRequest
    from mindroom.llm.runs import AnyRunRecord, RunRequirement
    from mindroom.tool_system.tool_function import ToolkitClose

logger = get_logger(__name__)

type Persist[R] = Callable[[R], Awaitable[None]]


class _StartedRunFields(TypedDict):
    """What every run record starts with, an agent's or a team's: its run, session, requester, model, and metadata."""

    run_id: str
    session_id: str
    parent_run_id: str | None
    user_id: str | None
    model: str
    model_provider: str
    metrics: RunMetrics
    metadata: dict[str, Any] | None


def started_run_fields(ctx: RunContext, model: ModelProvider, *, metadata: dict[str, Any] | None) -> _StartedRunFields:
    """Return the fields of the record a run in ``ctx`` on ``model`` starts with, storing ``metadata``."""
    identity = model.identity
    return _StartedRunFields(
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        parent_run_id=ctx.parent_run_id,
        user_id=ctx.user_id,
        model=identity.id,
        model_provider=identity.provider,
        metrics=RunMetrics(),
        metadata=metadata,
    )


class ApprovedCall(Protocol):
    """One paused call an approval authorized: the call, the function it paused on, and the arguments it binds."""

    @property
    def tool_call_id(self) -> str:
        """The paused call's id."""
        ...

    @property
    def tool_name(self) -> str:
        """The function the call paused on."""
        ...

    def binds_arguments(self, tool_args: Mapping[str, object] | None) -> bool:
        """Return whether these are exactly the arguments the approval was granted for."""
        ...


def run_agent(
    spec: AgentSpec,
    run_input: Sequence[Message],
    *,
    ctx: RunContext,
    persist: Persist[RunRecord] | None,
    request_template: ModelRequest,
    hooks: LoopHooks = NO_HOOKS,
    metadata: dict[str, Any] | None = None,
) -> AsyncIterator[RunEvent]:
    """Run one agent turn: the spec's system messages, stable then turn, then ``run_input`` as history built it.

    ``metadata`` is stored on the run's record; the run's tool calls read ``ctx.metadata``, which a team's member runs
    share with the team run while their records store none of it.
    The returned stream yields the loop's events and finally ``RunFinished`` with the ``COMPLETED`` or ``PAUSED``
    record, after ``persist`` stored it. Cancellation stores a ``CANCELLED`` record and re-raises; closing the stream
    before it finished stores one before ``aclose`` returns; any other exception stores an ``ERROR`` record, ends the
    stream with it as ``RunFinished``, then re-raises. The ``ERROR`` record's ``content`` keeps what the model wrote,
    and its ``error`` holds the exception's text with credentials redacted.
    """
    record = RunRecord(
        **started_run_fields(ctx, spec.model, metadata=metadata),
        agent_id=spec.id,
        agent_display_name=spec.name,
    )
    messages = [*spec.system_messages, *run_input]
    return drive_run(
        spec,
        record,
        messages,
        ctx=ctx,
        hooks=hooks,
        persist=persist,
        template=request_template,
    )


async def fail_unbuilt_run(
    ctx: RunContext,
    run_input: Sequence[Message],
    *,
    agent_id: str,
    agent_display_name: str,
    error: Exception,
    persist: Persist[RunRecord] | None,
) -> RunRecord:
    """End the run of an agent that could not be built, such as a team member, as ``ERROR`` and return its record.

    The run reached no model, so its record names none, stores ``run_input`` alone, and has no content; otherwise it
    ends as a run that fails does, ``error``'s text with credentials redacted as its ``error``, stored before this
    returns.
    """
    record = RunRecord(
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        parent_run_id=ctx.parent_run_id,
        user_id=ctx.user_id,
        metrics=RunMetrics(),
        agent_id=agent_id,
        agent_display_name=agent_display_name,
    )
    await _store_interrupted(record, run_input, time.monotonic(), persist, error=error)
    return record


def resume_agent(
    spec: AgentSpec,
    paused: RunRecord,
    messages: list[Message],
    *,
    approved: Sequence[ApprovedCall],
    denied: Mapping[str, str | None],
    external_results: Mapping[str, str],
    context: Callable[[str], RunContext],
    hooks: LoopHooks,
    persist: Persist[RunRecord] | None,
    request_template: ModelRequest,
) -> AsyncIterator[RunEvent]:
    """Resume a paused run with the decisions on exactly the calls it waits for, then continue its loop.

    ``messages`` is the resume request the history subsystem built from ``paused``, and ``context`` returns the
    context of the run with a given id, which the resume calls with ``paused``'s. Every call the record waits for
    must be covered once: a confirmation by one approved call whose name and argument digest match or by a denial,
    an external execution by its result; anything else raises ``ValueError`` here, before any call runs, and
    ``paused`` is left unchanged. Approved calls run from their stored arguments and announce themselves as tool
    events; denied calls answer with their reason, or the default note when it is None or empty; external results
    answer as they are. The returned stream ends as ``run_agent``'s does.
    """
    plan = plan_resume(spec, paused, approved=approved, denied=denied, external_results=external_results)
    return resume_planned(
        spec,
        plan,
        messages,
        context=context,
        hooks=hooks,
        persist=persist,
        request_template=request_template,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ResumePlan:
    """A resume whose decisions were checked and recorded: the record to continue and the calls it answers."""

    record: RunRecord
    decided: tuple[RunRequirement, ...]


def plan_resume(
    spec: AgentSpec,
    paused: RunRecord,
    *,
    approved: Sequence[ApprovedCall],
    denied: Mapping[str, str | None],
    external_results: Mapping[str, str],
) -> ResumePlan:
    """Check and record the decisions for a resume of ``paused`` without running anything; see ``resume_agent``.

    A team resumes several members from one approval, so it plans every member before any of them runs.
    """
    record = resumed_record(paused)
    decided = decide_requirements(record, approved=approved, denied=denied, external_results=external_results)
    check_approved_tools_offered(spec, approved)
    return ResumePlan(record=record, decided=tuple(decided))


def resume_planned(
    spec: AgentSpec,
    plan: ResumePlan,
    messages: list[Message],
    *,
    context: Callable[[str], RunContext],
    hooks: LoopHooks,
    persist: Persist[RunRecord] | None,
    request_template: ModelRequest,
) -> AsyncIterator[RunEvent]:
    """Run a planned resume: answer the decided calls, then continue the loop over ``messages``.

    The run's context is ``context`` of the planned record's run id.
    """
    return drive_run(
        spec,
        plan.record,
        messages,
        ctx=context(plan.record.run_id),
        hooks=hooks,
        persist=persist,
        template=request_template,
        resumed=plan.decided,
    )


def check_approved_tools_offered(spec: AgentSpec, approved: Sequence[ApprovedCall]) -> None:
    """Refuse a resume with an approved call whose tool the spec no longer offers."""
    for call in approved:
        if call.tool_name not in spec.functions:
            msg = f"Approved tool {call.tool_name!r} is no longer offered to agent {spec.id!r}"
            raise ValueError(msg)


def resumed_record[R: AnyRunRecord](paused: R) -> R:
    """Return the record the resume continues: a running copy of ``paused`` without its messages.

    The copy records its content as what the run carried from its pause (``carried_content``), and the loop joins what
    the model writes after the resume to it: a team leader or delegating parent reads both, while the reply the resume
    continues, which already shows the text written before the pause, adds only the rest (``new_handover_content``).
    The copy is deep, so no field it carries is shared with ``paused`` and a refused resume changes nothing; a team
    record keeps its own copy of the member runs that ended before the pause and of its paused member runs.
    """
    if paused.status is not RunStatus.paused:
        msg = f"Only a paused run can be resumed; run {paused.run_id!r} is {paused.status.value}"
        raise ValueError(msg)
    record = deepcopy(replace(paused, messages=[]))
    record.carried_content = record.content
    record.status = RunStatus.running
    if record.metrics is None:
        record.metrics = RunMetrics()
    return record


def decide_requirements(
    record: AnyRunRecord,
    *,
    approved: Sequence[ApprovedCall],
    denied: Mapping[str, str | None],
    external_results: Mapping[str, str],
) -> list[RunRequirement]:
    """Record the decisions on the calls the record waits for and return their requirements, in record order.

    Anything but exact coverage of the waiting calls is refused before any decision is recorded.
    """
    requirements = {requirement.tool_execution.tool_call_id: requirement for requirement in record.requirements}
    confirmations = {call_id for call_id, item in requirements.items() if item.tool_execution.requires_confirmation}
    externals = {call_id for call_id, item in requirements.items() if item.tool_execution.external_execution_required}
    approved_ids = [call.tool_call_id for call in approved]
    decided = {*approved_ids, *denied}
    if (
        len(approved_ids) != len(set(approved_ids))
        or set(approved_ids) & set(denied)
        or decided != confirmations
        or set(external_results) != externals
    ):
        msg = f"Decisions do not cover exactly the calls run {record.run_id!r} waits for; retry the request"
        raise ValueError(msg)
    for call in approved:
        tool = requirements[call.tool_call_id].tool_execution
        if tool.tool_name != call.tool_name or not call.binds_arguments(tool.tool_args):
            msg = f"Approved call {call.tool_call_id!r} no longer matches the paused call; retry the request"
            raise ValueError(msg)
    for call in approved:
        requirements[call.tool_call_id].confirm()
    for call_id, note in denied.items():
        requirements[call_id].reject(note)
    for call_id, result in external_results.items():
        requirements[call_id].set_external_execution_result(result)
    return list(requirements.values())


async def drive_run[R: AnyRunRecord](
    spec: AgentSpec,
    record: R,
    messages: list[Message],
    *,
    ctx: RunContext,
    hooks: LoopHooks,
    persist: Persist[R] | None,
    template: ModelRequest,
    entry: Callable[[], Awaitable[None]] | None = None,
    resumed: Sequence[RunRequirement] | None = None,
) -> AsyncIterator[RunEvent]:
    """Run ``entry`` and then the loop over ``messages``, ending the record exactly once however the run ends.

    ``spec`` supplies the model, the tools (its ``functions``), and the tool-call limit; ``record`` is the agent
    or team run the loop fills; ``entry`` is work the run does before the loop (a team resume's paused members), and
    ``resumed`` holds a resume's decided requirements, which the loop answers before its first request. The spec's
    toolkits that hold a connection for one run are connected before ``entry`` and closed after the run reports its
    end, once the consumer reads past ``RunFinished`` (into the failure, for a run that failed) or closes the stream;
    a cancelled run closes them once its record is stored.
    """
    # The clock reading the run's duration counts from: a resume continues the paused run's duration, and however this
    # leg ends, once or again after a terminal store fails, its duration is the time since then.
    carried = record.metrics.duration if record.metrics is not None else None
    started = time.monotonic() - (carried or 0.0)
    paused = False
    closes: list[ToolkitClose] = []
    try:
        await connect_toolkits(spec.toolkits, closes)
        if entry is not None:
            await entry()
        loop = run_model_loop(
            spec.model,
            messages,
            PreparedTools(functions=spec.functions),
            record=record,
            run=ctx,
            hooks=hooks,
            max_tool_calls_per_turn=spec.max_tool_calls_per_turn,
            request_template=template,
            resumed=resumed,
        )
        async with closing_async_stream(loop):
            async for event in loop:
                if isinstance(event, LoopEnded):
                    paused = event.paused
                else:
                    yield event
        _end(record, messages, started, RunStatus.paused if paused else RunStatus.completed)
        if persist is not None:
            await persist(record)
    except (asyncio.CancelledError, GeneratorExit):
        # GeneratorExit is the consumer closing the run while it handles an event, where a stop lands when a reply
        # is being sent; the usage the loop settled must reach the stored run before the caller writes its own record.
        await _store_interrupted(record, messages, started, persist, error=None)
        raise
    except Exception as error:
        await _store_interrupted(record, messages, started, persist, error=error)
        # A failed run's record ends its stream as a finished run's does, so a reader such as a delegated child's
        # observer takes every record it can report from the stream; the failure follows on the next read.
        yield RunFinished(record)
        raise
    else:
        # Reported before the toolkits close, so a consumer handles the finished run before a slow close, such as a
        # stdio MCP server's exit, begins; one that reads the end of the stream, such as a delegation's, still waits
        # through it.
        yield RunFinished(record)
    finally:
        await close_toolkits(closes)


async def _store_interrupted[R: AnyRunRecord](
    record: R,
    messages: Sequence[Message],
    started: float,
    persist: Persist[R] | None,
    *,
    error: Exception | None,
) -> None:
    """End a cancelled (``error`` None) or failed run and store it before its ending propagates.

    Cancellation wins over a pause: paused tool entries stop waiting. A failed run records its ``failure_text`` in
    ``error``, redacted, which is what its caller reports, and keeps in its content what its model wrote. The store
    finishes even if the waiter is cancelled again, so a record the caller writes for the same run afterwards is never
    overwritten by this one.
    """
    if error is None:
        _end(record, messages, started, RunStatus.cancelled)
        for tool in record.tools or ():
            if tool.is_paused:
                tool.requires_confirmation = False
                tool.external_execution_required = False
    else:
        _end(record, messages, started, RunStatus.error)
        record.error = redact_sensitive_text(failure_text(error))
    # A failed or cancelled approval resume stores this record over the paused run's row on purpose. Such a resume
    # ends its approval anyway: its cards expire and the approval is finished. A row kept paused would stay a turn
    # waiting for approval, and approving it again would rerun a call this record shows already ran.
    if persist is not None:
        await run_coroutine_until_complete(_persist_quietly(persist, record))


def _end(record: AnyRunRecord, messages: Sequence[Message], started: float, status: RunStatus) -> None:
    """Snapshot the live messages the run stores, stamp its status, and set its duration, counted from ``started``.

    Replayed history never stores. The turn context the run's requests sent (``add_to_agent_memory=False``), such as
    memory results and the approval receipt, stores in place: a resume then sends the paused request unchanged, and
    the next turn's request can extend this run's last one whole, which append-only prompt caches such as Codex's
    need. A run that stopped after a call drops it instead: a model switch continues the turn in another attempt,
    which replays this run and sends the turn context again.
    """
    stopped = status is not RunStatus.paused and any(tool.stop_after_call for tool in record.tools or ())
    record.messages = [
        message for message in messages if not message.from_history and (message.add_to_agent_memory or not stopped)
    ]
    record.status = status
    assert record.metrics is not None
    record.metrics.duration = time.monotonic() - started


async def _persist_quietly[R: AnyRunRecord](persist: Persist[R], record: R) -> None:
    """Store a terminal record of a run that is already ending; a failed store must not hide why it ended."""
    try:
        await persist(record)
    except Exception:
        logger.exception("terminal_run_persist_failed", run_id=record.run_id, status=record.status.value)


def own_model_messages(record: AnyRunRecord) -> list[Message]:
    """Return each message the run's own model requests answered (``is_own_model_message``), oldest first.

    Tool-calling steps count; the thread's earlier replies that the run's input carried, stored with the run, do not.
    """
    return [message for message in record.messages if is_own_model_message(message)]


__all__ = [
    "ApprovedCall",
    "Persist",
    "ResumePlan",
    "check_approved_tools_offered",
    "decide_requirements",
    "drive_run",
    "fail_unbuilt_run",
    "own_model_messages",
    "plan_resume",
    "resume_agent",
    "resume_planned",
    "resumed_record",
    "run_agent",
    "started_run_fields",
]
