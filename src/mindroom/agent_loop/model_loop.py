# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The request and tool loop shared by agents, team leaders, and skill reviews.

One iteration sends the live message list to the model, appends the assistant message it answers with, runs the
tool calls it requested, and appends their results; the loop ends when the model answers without tool calls, when
a tool stops it, when the run waits for a decision, when a pre-request hook ends it, or when the run's request cap
is reached. A resume enters the loop with the paused batch's decided calls, which it answers as the rest of that
batch before any request. A run waits for a decision when a call of its own paused, or when a tool left an
unresolved requirement on the record, as a team leader's delegation does for a paused member. Every point MindRoom
patched into Agno's loop is a ``LoopHooks`` field or a parameter here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.tools import (
    PlannedCall,
    dispatch_tool_calls,
    media_follow_up,
    plan_tool_batch,
    tool_result_message,
)
from mindroom.llm.errors import incomplete_stream_error
from mindroom.llm.events import ContentDelta, RequestCompleted, ToolCompleted, ToolStarted
from mindroom.llm.messages import AGGREGATE_USAGE_KEY, MODEL_PROVENANCE_KEY, Message
from mindroom.llm.provider import ModelRequest, ReasoningDelta, ReportedUsage, TextDelta, TurnCompleted, UsageReported
from mindroom.llm.runs import RunRecord, RunRequirement
from mindroom.logging_config import get_logger
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from mindroom.agent_loop.spec import RunContext
    from mindroom.agent_loop.tools import PreparedTools, ToolBatch
    from mindroom.llm.provider import ModelIdentity, ModelProvider, StopReason, ToolSpec
    from mindroom.llm.runs import AnyRunRecord, ToolExecution
    from mindroom.llm.usage import MessageMetrics, RunMetrics
    from mindroom.tool_system.invocation import ToolMiddleware
    from mindroom.tool_system.tool_function import ToolFunction

logger = get_logger(__name__)

# Every tool-calling request spends at least one budgeted call, so a run within its budget needs at most budget + 1
# requests; one more lets the model answer after its first refused batch.
_EXTRA_MODEL_REQUESTS = 2
# The per-run request settings of callers that have none; the loop fills ``messages`` and ``tools`` per request.
EMPTY_REQUEST = ModelRequest(messages=())


@dataclass
class LoopState:
    """What a pre-request hook sees and may change before one model request.

    ``messages`` is the live list the loop sends and appends to, so a hook rewrites it in place; ``offered`` holds the
    functions that answer the run's calls; ``tools`` are the definitions this request offers, which start as the
    offered functions' own and which a hook may extend (the wait budget job admission accepts) or replace (a skill
    review offering the definitions of the request it forks); ``record`` is the running run, whose tool executions and requirements the loop
    fills as it goes; ``loop_start_ids`` are the ids of the messages the loop started with; ``request_template``
    carries the per-run request settings, which a hook may replace for the rest of the run (mid-turn compaction
    turning the provider's compaction off after it compacted as text).
    """

    messages: list[Message]
    offered: PreparedTools
    tools: tuple[ToolSpec, ...]
    run: RunContext
    record: AnyRunRecord
    request_index: int
    loop_start_ids: frozenset[str]
    request_template: ModelRequest


@dataclass(frozen=True, slots=True)
class SendRequest:
    """Send the request; ``messages`` replaces the outbound list for this request only, leaving the live list as is."""

    messages: Sequence[Message] | None = None


@dataclass(frozen=True, slots=True)
class EndRun:
    """End the loop before this request, as a completed run with the text produced so far."""


type RequestDecision = SendRequest | EndRun
type BeforeRequestHook = Callable[[LoopState], Awaitable[RequestDecision]]
type RewriteToolCalls = Callable[[list[dict[str, Any]], Mapping[str, ToolFunction]], list[dict[str, Any]]]
type AnswerToolCalls = Callable[[Message, StopReason | None], list[Message] | None]
type BeforeDispatchHook = Callable[[LoopState, Sequence[PlannedCall]], Awaitable[None]]
type AfterToolBatchHook = Callable[[list[Message], Sequence[Message]], Awaitable[None]]
type LoopEndHook = Callable[[LoopState, ModelRequest], None]


@dataclass(frozen=True, slots=True, kw_only=True)
class LoopHooks:
    """The extension points of one loop, each fired at a fixed stage.

    - ``before_request``: awaited in order before every model request, after the request cap; a hook may rewrite
      ``state.messages`` in place (mid-turn compaction), end the loop (participation, the skill review's budget),
      or send a projected message list for this request only (the approval receipt); the last projection wins.
    - ``answer_tool_calls``: sees each assistant message that requests tools together with the turn's stop reason;
      when it returns result messages, they answer the batch in place of running it (the output-limit policy).
    - ``rewrite_tool_calls``: rewrites an assistant message's tool calls before they are resolved to functions
      (portable tool search), and the rewritten calls are what the message stores.
    - ``before_dispatch``: awaited with the loop state, whose messages end with the batch's assistant message and
      whose record holds the batch's executions, and the planned calls before any call of the batch runs (minimal
      mode's checkpoint of the batch).
    - ``tool_middleware``: wraps every tool call the loop executes, outermost first (job admission, the hook bridge).
    - ``after_tool_batch``: awaited in order with the live messages and the batch's result messages after the
      results and any media follow-up are appended, before the loop decides to pause or continue; a batch whose
      result stops the run runs none (queued-message and job notices).
    - ``on_resume_entry``: awaited in order by a resume with the live messages and the decided calls' results after
      they are appended, before the loop continues; a resumed batch whose result stops the run runs none.
    - ``on_loop_end``: called once when the loop ends without an exception, with the final state and the last
      request as sent, when any was (the skill review's capture).
    """

    before_request: tuple[BeforeRequestHook, ...] = ()
    answer_tool_calls: AnswerToolCalls | None = None
    rewrite_tool_calls: RewriteToolCalls | None = None
    before_dispatch: BeforeDispatchHook | None = None
    tool_middleware: tuple[ToolMiddleware, ...] = ()
    after_tool_batch: tuple[AfterToolBatchHook, ...] = ()
    on_resume_entry: tuple[AfterToolBatchHook, ...] = ()
    on_loop_end: LoopEndHook | None = None


NO_HOOKS = LoopHooks()


@dataclass(frozen=True, slots=True, kw_only=True)
class LoopEnded:
    """The loop's last event; ``paused`` says a call waits for a decision, so the run is paused rather than complete."""

    paused: bool


type LoopEvent = ContentDelta | ReasoningDelta | ToolStarted | ToolCompleted | RequestCompleted | LoopEnded


@dataclass
class _Progress:
    """What one loop accumulates across its iterations."""

    text_parts: list[str] = field(default_factory=list)
    calls_so_far: int = 0
    last_request: ModelRequest | None = None
    paused: bool = False
    stopped: bool = False


async def run_model_loop(
    model: ModelProvider,
    messages: list[Message],
    tools: PreparedTools,
    *,
    record: AnyRunRecord,
    run: RunContext,
    hooks: LoopHooks = NO_HOOKS,
    tool_call_limit: int | None = None,
    request_template: ModelRequest = EMPTY_REQUEST,
    resumed: Sequence[RunRequirement] | None = None,
) -> AsyncIterator[LoopEvent]:
    """Request, run the requested tools, and repeat until the run completes or pauses; ``LoopEnded`` comes last.

    ``messages`` is the list the run stores from: every assistant and tool message is appended to it.
    ``record`` collects the loop's text as ``content`` (the loop's requests concatenated, replacing what the record
    held), its tool executions, its pause requirements, and its usage, in the metrics the record must carry.
    ``request_template`` carries the per-run request settings; its messages and tools are replaced per request.
    ``resumed`` makes the loop a resume's: the requirements the resume decided, which it answers first, in order, as
    the rest of the paused batch; None for a fresh run.
    Provider errors, tool-call errors other than a tool's own exception, and cancellation propagate after the
    usage a cut-off request already reported is recorded on an assistant message, exactly once.
    """
    identity = model.identity
    metrics = record.metrics
    assert metrics is not None
    state = LoopState(
        messages=messages,
        offered=tools,
        tools=tools.specs(),
        run=run,
        record=record,
        request_index=0,
        loop_start_ids=frozenset(message.id for message in messages),
        request_template=request_template,
    )
    max_requests = None if tool_call_limit is None else tool_call_limit + _EXTRA_MODEL_REQUESTS
    progress = _Progress()
    if resumed is not None:
        async for event in _answer_resumed(resumed, state, progress, hooks):
            yield event
    while not (progress.stopped or progress.paused):
        outbound = await _before_request(hooks, state, max_requests=max_requests)
        if outbound is None:
            break
        request = replace(state.request_template, messages=tuple(outbound), tools=state.tools)
        progress.last_request = request
        turn: TurnCompleted | None = None
        events = _stream_turn(model, request, messages, metrics)
        async with closing_async_stream(events):
            async for event in events:
                # No await or break between taking the turn and asking for the next event: _stream_turn settles
                # the usage of a turn it still holds, and _accept_turn counts a handed-off one.
                if isinstance(event, TurnCompleted):
                    turn = event
                else:
                    yield event
        assert turn is not None
        message = _accept_turn(turn.message, state, progress, metrics, identity)
        yield RequestCompleted(model_id=identity.id, provider=identity.provider_name, usage=message.metrics)
        if not message.tool_calls:
            break
        async for event in _run_tool_batch(message, turn.stop_reason, state, progress, hooks, tool_call_limit):
            yield event
    if hooks.on_loop_end is not None and progress.last_request is not None:
        hooks.on_loop_end(state, progress.last_request)
    yield LoopEnded(paused=progress.paused)


async def _before_request(
    hooks: LoopHooks,
    state: LoopState,
    *,
    max_requests: int | None,
) -> Sequence[Message] | None:
    """Apply the request cap, then the pre-request hooks; return the messages to send, or None to end the loop."""
    if max_requests is not None and state.request_index >= max_requests:
        record = state.record
        logger.warning(
            "tool_call_limit_reached",
            entity=record.agent_id if isinstance(record, RunRecord) else record.team_id,
            budget=max_requests - _EXTRA_MODEL_REQUESTS,
            model_requests=state.request_index,
        )
        return None
    outbound: Sequence[Message] = state.messages
    for hook in hooks.before_request:
        decision = await hook(state)
        if isinstance(decision, EndRun):
            return None
        if decision.messages is not None:
            outbound = decision.messages
    return outbound


def _accept_turn(
    message: Message,
    state: LoopState,
    progress: _Progress,
    metrics: RunMetrics,
    identity: ModelIdentity,
) -> Message:
    """Record one finished request: the assistant message, its usage, and its text as the run's content so far.

    A request that answers with tool calls and no text leaves the content as it was, as Agno's loop did, so a
    resumed run keeps its paused text until the model says something new.
    """
    _stamp_model(message, identity)
    state.messages.append(message)
    _count_request(metrics, message.metrics, identity)
    state.request_index += 1
    if message.content is not None:
        progress.text_parts.append(message.get_content_string())
        state.record.content = "".join(progress.text_parts)
    return message


async def _run_tool_batch(
    message: Message,
    stop_reason: StopReason | None,
    state: LoopState,
    progress: _Progress,
    hooks: LoopHooks,
    tool_call_limit: int | None,
) -> AsyncIterator[ToolStarted | ToolCompleted]:
    """Answer one assistant message's tool calls with what ``answer_tool_calls`` gives, else plan, announce, and run them."""
    assert message.tool_calls
    answers = None if hooks.answer_tool_calls is None else hooks.answer_tool_calls(message, stop_reason)
    if answers is not None:
        progress.stopped = await _answer_batch(state.messages, answers, hooks.after_tool_batch)
        return
    if hooks.rewrite_tool_calls is not None:
        message.tool_calls = hooks.rewrite_tool_calls(message.tool_calls, state.offered.functions)
    batch = plan_tool_batch(
        message.tool_calls,
        state.offered.functions,
        state.messages,
        calls_so_far=progress.calls_so_far,
        tool_call_limit=tool_call_limit,
    )
    _record_batch(state.record, batch)
    if hooks.before_dispatch is not None:
        await hooks.before_dispatch(state, batch.planned)
    for call in batch.planned:
        yield ToolStarted(call.execution)
    executed = await dispatch_tool_calls(batch.planned, run=state.run, middleware=hooks.tool_middleware)
    for call in batch.planned:
        if not call.execution.is_paused:
            yield ToolCompleted(call.execution)
    results = [*batch.refused, *executed]
    progress.calls_so_far += len(results)
    progress.stopped = await _answer_batch(state.messages, results, hooks.after_tool_batch)
    # A call of this batch paused, or a tool left a decision on the record (a delegation whose member paused).
    progress.paused = batch.paused or _waits_for_decision(state.record)


async def _answer_resumed(
    decided: Sequence[RunRequirement],
    state: LoopState,
    progress: _Progress,
    hooks: LoopHooks,
) -> AsyncIterator[ToolStarted | ToolCompleted]:
    """Answer the requirements a resume decided, in order, as the rest of the batch the run paused in.

    Approved calls run from their stored arguments and announce themselves as tool events; denied calls answer with
    their note; external results answer as they are. The run then stops, pauses, or continues as after any batch:
    a member that paused once more leaves the record waiting, so the loop sends no request.
    """
    results: list[Message] = []
    for requirement in decided:
        execution = requirement.tool_execution
        if requirement.confirmation is True:
            assert execution.tool_name is not None
            function = state.offered.functions[execution.tool_name]
            async for event in _run_approved(function, execution, state.run, hooks, results):
                yield event
        elif requirement.confirmation is False:
            execution.tool_call_error = True
            execution.requires_confirmation = False
            results.append(_rejection_message(execution))
        if requirement.external_execution_result is not None:
            results.append(tool_result_message(execution))
            execution.external_execution_required = False
    progress.stopped = await _answer_batch(state.messages, results, hooks.on_resume_entry)
    progress.paused = _waits_for_decision(state.record)


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
    run: RunContext,
    hooks: LoopHooks,
    results: list[Message],
) -> AsyncIterator[ToolStarted | ToolCompleted]:
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
    results.extend(await dispatch_tool_calls([planned], run=run, middleware=hooks.tool_middleware))
    yield ToolCompleted(execution)


def _waits_for_decision(record: AnyRunRecord) -> bool:
    return any(not requirement.is_resolved() for requirement in record.requirements or ())


async def _answer_batch(
    messages: list[Message],
    results: Sequence[Message],
    hooks: Sequence[AfterToolBatchHook],
) -> bool:
    """Append one batch's results and return whether the batch stops the run, running ``hooks`` only when it does not.

    Each result is one canonical tool message; media leave them first, so a wire that merges results never carries
    them, and reach the model on one user message after the results. Every batch path answers here, so one rule
    decides the stop: a batch stops the run when any of its results does, the results a resumed batch answered
    before its pause included, as Agno's loop stopped after such a batch. No later request reads what a hook would
    add after a batch that stops the run, so none runs.
    """
    follow_up = media_follow_up(results)
    messages.extend(results)
    if follow_up is not None:
        messages.append(follow_up)
    if _batch_stops_run(messages):
        return True
    for hook in hooks:
        await hook(messages, results)
    return False


def _batch_stops_run(messages: Sequence[Message]) -> bool:
    """Return whether a result of the batch the live messages end with stops the run.

    The batch's results are the tool messages after its assistant message, whichever entry answered them.
    """
    for message in reversed(messages):
        if message.role == "assistant":
            return False
        if message.role == "tool" and message.stop_after_tool_call:
            return True
    return False


async def _stream_turn(
    model: ModelProvider,
    request: ModelRequest,
    messages: list[Message],
    metrics: RunMetrics,
) -> AsyncIterator[ContentDelta | ReasoningDelta | TurnCompleted]:
    """Stream one request, yielding its deltas and then its finished turn.

    When the stream is cut off by cancellation, a provider error, or a consumer that stops reading, the usage it
    already reported is kept on an assistant message appended to ``messages`` and counted once in ``metrics``,
    before whatever ended the stream reaches the run's terminal handling. A turn that completed but was never taken
    by the consumer, such as one cancelled while a pipeline stage finishes the stream, is cut off the same way with
    the turn's own usage; once the consumer takes it, ``_accept_turn`` counts that usage instead.
    """
    identity = model.identity
    turn: TurnCompleted | None = None
    handed_off = False
    reported = ReportedUsage()
    try:
        stream = model.stream(request)
        async with closing_async_stream(stream):
            async for event in stream:
                if isinstance(event, TextDelta):
                    yield ContentDelta(event.text)
                elif isinstance(event, ReasoningDelta):
                    yield event
                elif isinstance(event, UsageReported):
                    reported.observe(event)
                elif isinstance(event, TurnCompleted):
                    turn = event
        if turn is None:
            raise incomplete_stream_error(provider=identity.provider_name)
        yield turn
        # The consumer asked for more, so it holds the turn and accepts it before its next suspension point.
        handed_off = True
    finally:
        if not handed_off:
            # A cut-off request settles exactly what was billed: the completed turn's usage, else the final reports
            # plus a partial none replaced.
            billed = turn.message.metrics if turn is not None else reported.received
            if billed is not None and billed.to_dict():
                cut_off = Message(role="assistant", metrics=billed)
                if turn is not None and (turn.message.provider_data or {}).get(AGGREGATE_USAGE_KEY) is True:
                    # The turn's usage sums retried attempts, which must not read as one request's.
                    cut_off.provider_data = {AGGREGATE_USAGE_KEY: True}
                _stamp_model(cut_off, identity)
                messages.append(cut_off)
                _count_request(metrics, billed, identity)


def _stamp_model(message: Message, identity: ModelIdentity) -> None:
    """Record which model answered on the message, the key usage attribution reads (``usage/storage.py``)."""
    message.provider_data = {
        **(message.provider_data or {}),
        MODEL_PROVENANCE_KEY: {"id": identity.id, "provider": identity.provider_name},
    }


def _count_request(metrics: RunMetrics, usage: MessageMetrics, identity: ModelIdentity) -> None:
    metrics.add(usage, model_id=identity.id, provider=identity.provider_name)
    if metrics.time_to_first_token is None:
        metrics.time_to_first_token = usage.time_to_first_token


def _record_batch(record: AnyRunRecord, batch: ToolBatch) -> None:
    """Add the batch's executions to the record, and a requirement for each paused one."""
    if record.tools is None:
        record.tools = []
    for call in batch.planned:
        record.tools.append(call.execution)
        if call.execution.is_paused:
            if record.requirements is None:
                record.requirements = []
            record.requirements.append(RunRequirement(call.execution))


__all__ = [
    "EMPTY_REQUEST",
    "NO_HOOKS",
    "AfterToolBatchHook",
    "AnswerToolCalls",
    "BeforeDispatchHook",
    "BeforeRequestHook",
    "EndRun",
    "LoopEndHook",
    "LoopEnded",
    "LoopEvent",
    "LoopHooks",
    "LoopState",
    "RequestDecision",
    "RewriteToolCalls",
    "SendRequest",
    "run_model_loop",
]
