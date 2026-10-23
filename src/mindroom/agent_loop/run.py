# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Agent runs and approval resumes over the model loop.

A run assembles its messages, drives the loop, and ends in exactly one terminal record: ``COMPLETED`` or ``PAUSED``
through ``RunFinished``, ``CANCELLED`` or ``ERROR`` through the exception that ended it. Each terminal record is
handed to the persist callback once, before the run yields or raises its ending, so a caller that writes its own
record for the same run afterwards always writes last.

``drive_run``, ``resumed_record``, and ``decide_requirements`` are the pieces an agent run and a resume are made of;
the team runtime drives a team leader's record through the same pieces.
"""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from mindroom.agent_loop.model_loop import EMPTY_REQUEST, NO_HOOKS, LoopEnded, LoopHooks, run_model_loop
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.llm.events import RunFinished
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import RunMetrics
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.tool_function import close_toolkits, connect_toolkits

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from mindroom.agent_loop.spec import AgentSpec, RunContext
    from mindroom.agent_loop.tools import PreparedTools
    from mindroom.llm.events import RunEvent
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelRequest
    from mindroom.llm.runs import AnyRunRecord, RunRequirement, ToolExecution
    from mindroom.tool_system.tool_function import ToolkitClose

logger = get_logger(__name__)

type Persist[R] = Callable[[R], Awaitable[None]]


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
    hooks: LoopHooks = NO_HOOKS,
    persist: Persist[RunRecord] | None = None,
    request_template: ModelRequest = EMPTY_REQUEST,
) -> AsyncIterator[RunEvent]:
    """Run one agent turn: the spec's system message, then ``run_input`` as the history subsystem built it.

    The returned stream yields the loop's events and finally ``RunFinished`` with the ``COMPLETED`` or ``PAUSED``
    record, after ``persist`` stored it. Cancellation stores a ``CANCELLED`` record and re-raises; closing the stream
    before it finished stores one before ``aclose`` returns; any other exception stores an ``ERROR`` record whose
    content is the exception's text when the model had said nothing, then re-raises. The ``ERROR`` record's
    ``error`` holds the exception's text with credentials redacted.
    """
    identity = spec.model.identity
    record = RunRecord(
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        parent_run_id=ctx.parent_run_id,
        user_id=ctx.user_id,
        model=identity.id,
        model_provider=identity.provider,
        metrics=RunMetrics(),
        metadata=ctx.metadata,
        agent_id=spec.id,
        agent_name=spec.name,
    )
    messages = [*([] if spec.system_message is None else [spec.system_message]), *run_input]
    return drive_run(
        spec,
        record,
        messages,
        prepare_run_tools(spec),
        ctx=ctx,
        hooks=hooks,
        persist=persist,
        template=request_template,
    )


def resume_agent(
    spec: AgentSpec,
    paused: RunRecord,
    messages: list[Message],
    *,
    approved: Sequence[ApprovedCall] = (),
    denied: Mapping[str, str] = {},
    external_results: Mapping[str, str] = {},
    ctx: RunContext,
    hooks: LoopHooks = NO_HOOKS,
    persist: Persist[RunRecord] | None = None,
    request_template: ModelRequest = EMPTY_REQUEST,
) -> AsyncIterator[RunEvent]:
    """Resume a paused run with the decisions on exactly the calls it waits for, then continue its loop.

    ``messages`` is the resume request the history subsystem built from ``paused``. Every call the record waits for
    must be covered once: a confirmation by one approved call whose name and argument digest match or by a denial,
    an external execution by its result; anything else raises ``ValueError`` here, before any call runs, and
    ``paused`` is left unchanged. Approved calls run from their stored arguments and announce themselves as tool
    events; denied calls answer with their note; external results answer as they are. The returned stream ends as ``run_agent``'s does.
    """
    plan = plan_resume(spec, paused, approved=approved, denied=denied, external_results=external_results)
    return resume_planned(
        spec,
        plan,
        messages,
        ctx=ctx,
        hooks=hooks,
        persist=persist,
        request_template=request_template,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ResumePlan:
    """A resume whose decisions were checked and recorded: the record to continue, what it answers, what it offers."""

    record: RunRecord
    decided: tuple[RunRequirement, ...]
    prepared: PreparedTools


def plan_resume(
    spec: AgentSpec,
    paused: RunRecord,
    *,
    approved: Sequence[ApprovedCall] = (),
    denied: Mapping[str, str] = {},
    external_results: Mapping[str, str] = {},
) -> ResumePlan:
    """Check and record the decisions for a resume of ``paused`` without running anything; see ``resume_agent``.

    A team resumes several members from one approval, so it plans every member before any of them runs.
    """
    record = resumed_record(paused)
    decided = decide_requirements(record, approved=approved, denied=denied, external_results=external_results)
    return ResumePlan(record=record, decided=tuple(decided), prepared=prepare_resume_tools(spec, approved))


def resume_planned(
    spec: AgentSpec,
    plan: ResumePlan,
    messages: list[Message],
    *,
    ctx: RunContext,
    hooks: LoopHooks = NO_HOOKS,
    persist: Persist[RunRecord] | None = None,
    request_template: ModelRequest = EMPTY_REQUEST,
) -> AsyncIterator[RunEvent]:
    """Run a planned resume: answer the decided calls, then continue the loop over ``messages``."""
    return drive_run(
        spec,
        plan.record,
        messages,
        plan.prepared,
        ctx=ctx,
        hooks=hooks,
        persist=persist,
        template=request_template,
        resumed=plan.decided,
    )


def prepare_resume_tools(spec: AgentSpec, approved: Sequence[ApprovedCall]) -> PreparedTools:
    """Prepare the tools a resume offers, refusing an approved call whose tool the spec no longer offers."""
    prepared = prepare_run_tools(spec)
    for call in approved:
        if call.tool_name not in prepared.functions:
            msg = f"Approved tool {call.tool_name!r} is no longer offered to agent {spec.id!r}"
            raise ValueError(msg)
    return prepared


def resumed_record[R: AnyRunRecord](paused: R) -> R:
    """Return the record the resume continues: a running copy of ``paused`` without its messages.

    The copy is deep, so no field it carries is shared with ``paused`` and a refused resume changes nothing; a team
    record keeps its own copy of the member runs that ended before the pause, as Agno's continuation of its live run
    kept them. The copy's own requirements share their tool entries: Agno's continuation replaced each tool entry by
    the requirement's execution of the same call, so a decision recorded on a requirement shows on the run's tools
    too. A team run also holds its members' requirements, whose call ids come from other provider requests and may
    equal a leader call's id; those belong to the member's run and never replace the leader's entries. A member
    record resumed on its own owns its requirements too: the team runtime names them with that record's run id
    (``_plan_member_resumes``), so the ``member_run_id`` half of the check keeps the aliasing on the member's tools.
    """
    if paused.status is not RunStatus.paused:
        msg = f"Only a paused run can be resumed; run {paused.run_id!r} is {paused.status.value}"
        raise ValueError(msg)
    record = deepcopy(replace(paused, messages=[]))
    record.status = RunStatus.running
    record.requirements = record.requirements or []
    if record.metrics is None:
        record.metrics = RunMetrics()
    by_call = {
        requirement.tool_execution.tool_call_id: requirement.tool_execution
        for requirement in record.requirements
        if requirement.tool_execution.tool_call_id is not None
        and (requirement.member_agent_id is None or requirement.member_run_id == paused.run_id)
    }
    record.tools = [by_call.get(tool.tool_call_id) or tool for tool in record.tools or []]
    return record


def decide_requirements(
    record: AnyRunRecord,
    *,
    approved: Sequence[ApprovedCall],
    denied: Mapping[str, str],
    external_results: Mapping[str, str],
) -> list[RunRequirement]:
    """Apply the decisions to the calls the record waits for and return their requirements, in record order.

    Anything but exact coverage of the waiting calls is refused before any decision is recorded.
    """
    requirements = {
        requirement.tool_execution.tool_call_id: requirement
        for requirement in record.requirements or ()
        if requirement.tool_execution.tool_call_id is not None
    }
    waiting = [*(record.tools or ()), *(requirement.tool_execution for requirement in record.requirements or ())]
    confirmations = {tool.tool_call_id for tool in waiting if tool.requires_confirmation}
    externals = {tool.tool_call_id for tool in waiting if tool.external_execution_required}
    approved_ids = [call.tool_call_id for call in approved]
    decided = {*approved_ids, *denied}
    if (
        len(approved_ids) != len(set(approved_ids))
        or set(approved_ids) & set(denied)
        or decided != confirmations
        or set(external_results) != externals
        or not (confirmations | externals) <= set(requirements)
    ):
        msg = f"Decisions do not cover exactly the calls run {record.run_id!r} waits for; retry the request"
        raise ValueError(msg)
    for call in approved:
        requirement = requirements[call.tool_call_id]
        tool = requirement.tool_execution
        if tool.tool_name != call.tool_name or not call.binds_arguments(tool.tool_args):
            msg = f"Approved call {call.tool_call_id!r} no longer matches the paused call; retry the request"
            raise ValueError(msg)
    for call in approved:
        requirements[call.tool_call_id].confirm()
    for call_id, note in denied.items():
        requirements[call_id].reject(note)
    for call_id, result in external_results.items():
        requirements[call_id].set_external_execution_result(result)
    waited_for = confirmations | externals
    return [requirement for call_id, requirement in requirements.items() if call_id in waited_for]


def _agent_paused_content(record: AnyRunRecord) -> str:
    """Return Agno's text for an agent run paused on its own tools."""
    return paused_content(record.tools or ())


async def drive_run[R: AnyRunRecord](
    spec: AgentSpec,
    record: R,
    messages: list[Message],
    prepared: PreparedTools,
    *,
    ctx: RunContext,
    hooks: LoopHooks,
    persist: Persist[R] | None,
    template: ModelRequest,
    entry: Callable[[], Awaitable[None]] | None = None,
    resumed: Sequence[RunRequirement] | None = None,
    pause_content: Callable[[R], str] = _agent_paused_content,
) -> AsyncIterator[RunEvent]:
    """Run ``entry`` and then the loop over ``messages``, ending the record exactly once however the run ends.

    ``spec`` supplies the model and the tool-call limit; ``record`` is the agent or team run the loop fills;
    ``entry`` is work the run does before the loop (a team resume's paused members), and ``resumed`` holds a
    resume's decided requirements, which the loop answers before its first request. A run
    that ends paused without text of its own gets ``pause_content(record)`` as its content. The spec's toolkits that
    hold a connection for one run are connected before ``entry`` and closed after the run reports its end, as Agno's
    agent closed them after its completed event, once the consumer reads past ``RunFinished`` or closes the stream;
    a run that fails or is cancelled closes them once its record is stored.
    """
    started = time.monotonic()
    paused = False
    closes: list[ToolkitClose] = []
    try:
        await connect_toolkits(spec.toolkits, closes)
        if entry is not None:
            await entry()
        loop = run_model_loop(
            spec.model,
            messages,
            prepared,
            record=record,
            run=ctx,
            hooks=hooks,
            tool_call_limit=spec.tool_call_limit,
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
        if paused and not record.content:
            record.content = pause_content(record)
        if persist is not None:
            await persist(record)
    except (asyncio.CancelledError, GeneratorExit):
        # GeneratorExit is the consumer closing the run while it handles an event, where a stop lands when a reply
        # is being sent; the usage the loop settled must reach the stored run before the caller writes its own record.
        await _store_interrupted(record, messages, started, persist, error=None)
        raise
    except Exception as error:
        await _store_interrupted(record, messages, started, persist, error=error)
        raise
    else:
        # Reported before the toolkits close, as Agno did. A consumer that reads the end of the stream, such as a
        # delegation's, still waits through a slow close, such as a stdio MCP server's exit.
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

    Cancellation wins over a pause: unresolved requirements leave and paused tool entries stop waiting. A failed
    run records why in ``error``, redacted, which is what its caller reports; its stored content is the error's text
    only when its model said nothing, as Agno stored it. The store finishes even if the waiter is cancelled again,
    so a record the caller writes for the same run afterwards is never overwritten by this one.
    """
    if error is None:
        _end(record, messages, started, RunStatus.cancelled)
        if record.requirements:
            record.requirements = [requirement for requirement in record.requirements if requirement.is_resolved()]
        for tool in record.tools or ():
            if tool.is_paused:
                tool.requires_confirmation = False
                tool.external_execution_required = False
    else:
        _end(record, messages, started, RunStatus.error)
        record.error = redact_sensitive_text(str(error))
        if not record.content:
            record.content = str(error)
    if persist is not None:
        await run_coroutine_until_complete(_persist_quietly(persist, record))


def _end(record: AnyRunRecord, messages: Sequence[Message], started: float, status: RunStatus) -> None:
    """Snapshot the live messages the run stores, stamp its status, and close its duration."""
    record.messages = [message for message in messages if message.add_to_agent_memory and not message.from_history]
    record.status = status
    assert record.metrics is not None
    record.metrics.duration = time.monotonic() - started


async def _persist_quietly[R: AnyRunRecord](persist: Persist[R], record: R) -> None:
    """Store a terminal record of a run that is already ending; a failed store must not hide why it ended."""
    try:
        await persist(record)
    except Exception:
        logger.exception("terminal_run_persist_failed", run_id=record.run_id, status=record.status.value)


def paused_content(tools: Sequence[ToolExecution]) -> str:
    """Return the text of a paused run that produced none of its own: Agno's text while a call waits for confirmation.

    The only externally executed calls, delegations, pause silently and never wait for confirmation, so none of Agno's
    other paused texts applies.
    """
    if any(tool.requires_confirmation is True and not tool.confirmed for tool in tools):
        return "I have tools to execute, but I need confirmation."
    return ""


__all__ = [
    "ApprovedCall",
    "Persist",
    "ResumePlan",
    "decide_requirements",
    "drive_run",
    "paused_content",
    "plan_resume",
    "prepare_resume_tools",
    "resume_agent",
    "resume_planned",
    "resumed_record",
    "run_agent",
]
