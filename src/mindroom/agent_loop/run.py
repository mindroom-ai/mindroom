# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Agent runs and approval resumes over the model loop.

A run assembles its messages, drives the loop, and ends in exactly one terminal record: ``COMPLETED`` or ``PAUSED``
through ``RunFinished``, ``CANCELLED`` or ``ERROR`` through the exception that ended it. Each terminal record is
handed to the persist callback once, before the run yields or raises its ending, so a caller that writes its own
record for the same run afterwards always writes last.
"""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Protocol

from mindroom.agent_loop.model_loop import (
    EMPTY_REQUEST,
    NO_HOOKS,
    LoopEnded,
    LoopHooks,
    append_tool_results,
    run_model_loop,
)
from mindroom.agent_loop.tools import PlannedCall, dispatch_tool_calls, prepare_run_tools, tool_result_message
from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.llm.events import RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import RunMetrics
from mindroom.logging_config import get_logger
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from mindroom.agent_loop.spec import AgentSpec, RunContext
    from mindroom.agent_loop.tools import PreparedTools
    from mindroom.llm.events import RunEvent
    from mindroom.llm.provider import ModelRequest
    from mindroom.llm.runs import ToolExecution
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

# Agno's text for a denial that carries no note of its own.
REJECTED_TEXT = "Function call was rejected by the user"

type Persist = Callable[[RunRecord], Awaitable[None]]
type _Entry = Callable[[], AsyncIterator[RunEvent]]


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


async def run_agent(
    spec: AgentSpec,
    run_input: Sequence[Message],
    *,
    ctx: RunContext,
    hooks: LoopHooks = NO_HOOKS,
    persist: Persist | None = None,
    request_template: ModelRequest = EMPTY_REQUEST,
) -> AsyncIterator[RunEvent]:
    """Run one agent turn: the spec's system message, then ``run_input`` as the history subsystem built it.

    Yields the loop's events and finally ``RunFinished`` with the ``COMPLETED`` or ``PAUSED`` record, after
    ``persist`` stored it. Cancellation stores a ``CANCELLED`` record and re-raises; any other exception stores an
    ``ERROR`` record whose content is the exception's text when the model had said nothing, then re-raises.
    """
    identity = spec.model.identity
    record = RunRecord(
        run_id=ctx.run_id,
        session_id=ctx.session_id,
        user_id=ctx.user_id,
        model=identity.id,
        model_provider=identity.provider,
        metrics=RunMetrics(),
        metadata=ctx.metadata,
        agent_id=spec.id,
        agent_name=spec.name,
    )
    messages = [*([] if spec.system_message is None else [spec.system_message]), *run_input]
    async for event in _drive(
        spec,
        record,
        messages,
        prepare_run_tools(spec),
        ctx=ctx,
        hooks=hooks,
        persist=persist,
        template=request_template,
    ):
        yield event


async def resume_agent(
    spec: AgentSpec,
    paused: RunRecord,
    messages: list[Message],
    *,
    approved: Sequence[ApprovedCall] = (),
    denied: Mapping[str, str | None] = {},
    external_results: Mapping[str, str] = {},
    ctx: RunContext,
    hooks: LoopHooks = NO_HOOKS,
    persist: Persist | None = None,
    request_template: ModelRequest = EMPTY_REQUEST,
) -> AsyncIterator[RunEvent]:
    """Resume a paused run with the decisions on exactly the calls it waits for, then continue its loop.

    ``messages`` is the resume request the history subsystem built from ``paused``. Every call the record waits for
    must be covered once: a confirmation by one approved call whose name and argument digest match or by a denial,
    an external execution by its result; anything else raises ``ValueError`` before any call runs, and ``paused``
    is left unchanged. Approved calls run from their stored arguments and announce themselves as tool events;
    denied calls answer with their note (``REJECTED_TEXT`` without one); external results answer as they are.
    """
    record = _resumed_record(paused)
    _decide(record, approved=approved, denied=denied, external_results=external_results)
    prepared = prepare_run_tools(spec)
    for call in approved:
        if call.tool_name not in prepared.functions:
            msg = f"Approved tool {call.tool_name!r} is no longer offered to agent {spec.id!r}"
            raise ValueError(msg)
    async for event in _drive(
        spec,
        record,
        messages,
        prepared,
        ctx=ctx,
        hooks=hooks,
        persist=persist,
        template=request_template,
        entry=partial(_resume_entry, spec, record, messages, prepared, ctx, hooks),
    ):
        yield event


async def _resume_entry(
    spec: AgentSpec,
    record: RunRecord,
    messages: list[Message],
    prepared: PreparedTools,
    ctx: RunContext,
    hooks: LoopHooks,
) -> AsyncIterator[RunEvent]:
    """Answer every decided requirement in order, append the answers, and run the resume-entry hooks."""
    results: list[Message] = []
    for requirement in record.requirements or ():
        execution = requirement.tool_execution
        if requirement.confirmation is True:
            assert execution.tool_name is not None
            async for event in _run_approved(prepared.functions[execution.tool_name], execution, ctx, hooks, results):
                yield event
        elif requirement.confirmation is False:
            execution.tool_call_error = True
            results.append(_rejection_message(execution))
        if requirement.confirmation is not None:
            execution.requires_confirmation = False
        if requirement.external_execution_result is not None and execution.external_execution_required:
            results.append(tool_result_message(execution))
            execution.external_execution_required = False
    await append_tool_results(spec.model, messages, results, ())
    for hook in hooks.on_resume_entry:
        await hook(messages)


def _rejection_message(execution: ToolExecution) -> Message:
    """Return the tool message that answers a denied call with the stored note, as Agno's ``reject_tool_call`` did."""
    return Message(
        role="tool",
        content=execution.confirmation_note,
        tool_call_id=execution.tool_call_id,
        tool_name=execution.tool_name,
        tool_args=execution.tool_args,
        tool_call_error=True,
        stop_after_tool_call=execution.stop_after_tool_call,
    )


async def _run_approved(
    function: ToolFunction,
    execution: ToolExecution,
    ctx: RunContext,
    hooks: LoopHooks,
    results: list[Message],
) -> AsyncIterator[RunEvent]:
    assert execution.tool_call_id is not None
    # The decision has been taken: the entry no longer waits, so the dispatcher runs it.
    execution.requires_confirmation = False
    planned = PlannedCall(
        call_id=execution.tool_call_id,
        function=function,
        arguments=execution.tool_args,
        execution=execution,
    )
    yield ToolStarted(execution)
    results.extend(await dispatch_tool_calls([planned], run=ctx, middleware=hooks.tool_middleware))
    yield ToolCompleted(execution)


def _resumed_record(paused: RunRecord) -> RunRecord:
    """Return the record the resume continues: a copy of ``paused`` whose requirements share their tool entries.

    Agno's continuation replaced each tool entry by the requirement's execution of the same call, so a decision
    recorded on a requirement shows on the run's tools too; the copy keeps that aliasing.
    """
    if paused.status is not RunStatus.paused:
        msg = f"Only a paused run can be resumed; run {paused.run_id!r} is {paused.status.value}"
        raise ValueError(msg)
    requirements = deepcopy(paused.requirements or [])
    by_call = {
        requirement.tool_execution.tool_call_id: requirement.tool_execution
        for requirement in requirements
        if requirement.tool_execution.tool_call_id is not None
    }
    tools = [by_call.get(tool.tool_call_id) or deepcopy(tool) for tool in paused.tools or []]
    return replace(
        paused,
        status=RunStatus.running,
        messages=[],
        tools=tools,
        requirements=requirements,
        metrics=deepcopy(paused.metrics) or RunMetrics(),
        metadata=deepcopy(paused.metadata),
    )


def _decide(
    record: RunRecord,
    *,
    approved: Sequence[ApprovedCall],
    denied: Mapping[str, str | None],
    external_results: Mapping[str, str],
) -> None:
    """Apply the decisions to the record's requirements, refusing anything but exact coverage of its paused calls."""
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
        requirements[call_id].reject(note or REJECTED_TEXT)
    for call_id, result in external_results.items():
        requirements[call_id].set_external_execution_result(result)


async def _drive(
    spec: AgentSpec,
    record: RunRecord,
    messages: list[Message],
    prepared: PreparedTools,
    *,
    ctx: RunContext,
    hooks: LoopHooks,
    persist: Persist | None,
    template: ModelRequest,
    entry: _Entry | None = None,
) -> AsyncIterator[RunEvent]:
    """Run ``entry`` and then the loop over ``messages``, ending the record exactly once however the run ends."""
    started = time.monotonic()
    paused = False
    try:
        if entry is not None:
            entry_events = entry()
            async with closing_async_stream(entry_events):
                async for event in entry_events:
                    yield event
        loop = run_model_loop(
            spec.model,
            messages,
            prepared,
            record=record,
            run=ctx,
            hooks=hooks,
            tool_call_limit=spec.tool_call_limit,
            request_template=template,
        )
        async with closing_async_stream(loop):
            async for event in loop:
                if isinstance(event, LoopEnded):
                    paused = event.paused
                else:
                    yield event
        _end(record, messages, started, RunStatus.paused if paused else RunStatus.completed)
        if paused and not record.content:
            record.content = paused_content(record.tools or ())
        if persist is not None:
            await persist(record)
    except asyncio.CancelledError:
        await _store_interrupted(record, messages, started, persist, error=None)
        raise
    except Exception as error:
        await _store_interrupted(record, messages, started, persist, error=error)
        raise
    yield RunFinished(record)


async def _store_interrupted(
    record: RunRecord,
    messages: Sequence[Message],
    started: float,
    persist: Persist | None,
    *,
    error: Exception | None,
) -> None:
    """End a cancelled (``error`` None) or failed run and store it before its ending propagates.

    Cancellation wins over a pause: unresolved requirements leave and paused tool entries stop waiting. A failed
    run whose model said nothing holds the error's text. The store finishes even if the waiter is cancelled again,
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
        if not record.content:
            record.content = str(error)
    if persist is not None:
        await run_coroutine_until_complete(_persist_quietly(persist, record))


def _end(record: RunRecord, messages: Sequence[Message], started: float, status: RunStatus) -> None:
    """Snapshot the live messages the run stores, stamp its status, and close its duration."""
    record.messages = [message for message in messages if message.add_to_agent_memory and not message.from_history]
    record.status = status
    assert record.metrics is not None
    record.metrics.duration = time.monotonic() - started


async def _persist_quietly(persist: Persist, record: RunRecord) -> None:
    """Store a terminal record of a run that is already ending; a failed store must not hide why it ended."""
    try:
        await persist(record)
    except Exception:
        logger.exception("terminal_run_persist_failed", run_id=record.run_id, status=record.status.value)


def paused_content(tools: Sequence[ToolExecution]) -> str:
    """Return Agno's text for a paused run that produced none of its own; silent external calls say nothing."""
    content = ""
    for tool in tools:
        if tool.external_execution_silent:
            continue
        confirmation = tool.requires_confirmation is True and not tool.confirmed
        external = tool.external_execution_required is True
        if confirmation and external:
            content = "I have tools to execute, but I need confirmation or external execution."
        elif confirmation:
            content = "I have tools to execute, but I need confirmation."
        elif external:
            content = "I have tools to execute, but it needs external execution."
    return content


__all__ = [
    "REJECTED_TEXT",
    "ApprovedCall",
    "Persist",
    "paused_content",
    "resume_agent",
    "run_agent",
]
