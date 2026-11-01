# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""The request and tool loop shared by agents, team leaders, and skill reviews.

One iteration sends the live message list to the model, appends the assistant message it answers with, runs the
tool calls it requested, and appends their results; the loop ends when the model answers without tool calls, when
a tool stops it, when the run waits for a decision, when a pre-request hook ends it, or when the run's request cap
is reached. A resume enters the loop with the paused batch's decided calls, which it answers as the rest of that
batch before any request. A run waits for a decision when a call of its own paused, or when a tool left an
unresolved requirement on the record, as a team leader's delegation does for a paused member.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.tools import (
    PlannedCall,
    RefusedCall,
    answered_tool_batch,
    dispatch_tool_calls,
    failed_answer,
    media_follow_up,
    plan_tool_batch,
    tool_result_message,
)
from mindroom.llm.errors import incomplete_stream_error
from mindroom.llm.events import ToolCompleted, ToolProgress, ToolStarted
from mindroom.llm.messages import AGGREGATE_USAGE_KEY, Message, model_provenance, usage_sums_attempts
from mindroom.llm.provider import ModelRequest, ReasoningDelta, ReportedUsage, TextDelta, TurnCompleted, UsageReported
from mindroom.llm.runs import RunRecord, RunRequirement, joined_texts
from mindroom.logging_config import get_logger
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from mindroom.agent_loop.spec import RunContext
    from mindroom.agent_loop.tools import PreparedTools, ToolBatch
    from mindroom.llm.provider import ModelIdentity, ModelProvider, StopReason, ToolSpec
    from mindroom.llm.runs import AnyRunRecord
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
    review offering the definitions of the request it forks); ``record`` is the running run, whose tool executions,
    paused ones included, the loop fills as it goes; ``loop_start_ids`` are the ids of the messages the loop started
    with; ``request_template`` carries the per-run request settings, which a hook may replace for the rest of the run
    (mid-turn compaction turning the provider's compaction off after it compacted as text).
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
    """Send the request with the live messages as the hooks left them."""


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
      ``state.messages`` in place (mid-turn compaction), append to them (the approval receipt), or end the loop
      (participation, the skill review's budget).
    - ``answer_tool_calls``: sees each assistant message that requests tools together with the turn's stop reason;
      when it returns result messages, one per call in call order, they answer the batch in place of running it, and
      each call is recorded and announced as a failed call (the output-limit policy).
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


type LoopEvent = TextDelta | ReasoningDelta | ToolStarted | ToolProgress | ToolCompleted | LoopEnded


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
    hooks: LoopHooks,
    max_tool_calls_per_turn: int | None,
    request_template: ModelRequest,
    resumed: Sequence[RunRequirement] | None = None,
) -> AsyncIterator[LoopEvent]:
    """Request, run the requested tools, and repeat until the run completes or pauses; ``LoopEnded`` comes last.

    ``messages`` is the list the run stores from: every assistant and tool message is appended to it.
    ``record`` collects the loop's text as ``content`` (a resumed run's carried text and the loop's request texts, a
    blank line apart), its tool executions, paused ones included, and its usage, in the metrics the record must
    carry.
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
    max_requests = None if max_tool_calls_per_turn is None else max_tool_calls_per_turn + _EXTRA_MODEL_REQUESTS
    # A resume continues its run, so the calls that run already made count toward its limit; the refusals that never
    # counted stay out.
    progress = _Progress(calls_so_far=len(record.tools or ()) - record.uncounted_tool_calls)
    if resumed is not None:
        async for event in _answer_resumed(resumed, state, progress, hooks):
            yield event
    while not (progress.stopped or progress.paused):
        if not await _before_request(hooks, state, max_requests=max_requests):
            break
        request = replace(state.request_template, messages=tuple(state.messages), tools=state.tools)
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
        message = turn.message
        _accept_turn(message, state, progress, identity)
        if not message.tool_calls:
            break
        batch_events = _run_tool_batch(message, turn.stop_reason, state, progress, hooks, max_tool_calls_per_turn)
        # A batch yields while some of its calls still run, so a consumer that closes the run closes the batch too,
        # which stops those calls before the run stores its record.
        async with closing_async_stream(batch_events):
            async for event in batch_events:
                yield event
    if hooks.on_loop_end is not None and progress.last_request is not None:
        hooks.on_loop_end(state, progress.last_request)
    yield LoopEnded(paused=progress.paused)


async def _before_request(
    hooks: LoopHooks,
    state: LoopState,
    *,
    max_requests: int | None,
) -> bool:
    """Apply the request cap, then the pre-request hooks; return whether to send the request or end the loop."""
    if max_requests is not None and state.request_index >= max_requests:
        logger.warning(
            "tool_call_limit_reached",
            entity=_entity(state.record),
            budget=max_requests - _EXTRA_MODEL_REQUESTS,
            model_requests=state.request_index,
        )
        return False
    for hook in hooks.before_request:
        if isinstance(await hook(state), EndRun):
            return False
    return True


def _accept_turn(
    message: Message,
    state: LoopState,
    progress: _Progress,
    identity: ModelIdentity,
) -> None:
    """Record one finished request: the assistant message, its usage, and its text as the run's content so far.

    The provider stamped the message with the model that answered it. The run's content is the text a resumed run
    carried from its pause and its requests' texts, joined a blank line apart (``joined_texts``). A request that
    answers without content, as the adapters report a turn without text, leaves the content as it was.
    """
    metrics = state.record.metrics
    assert metrics is not None
    state.messages.append(message)
    _count_request(metrics, message.metrics, identity)
    state.request_index += 1
    if message.content is not None:
        progress.text_parts.append(message.get_content_string())
        state.record.content = joined_texts([state.record.carried_content or "", *progress.text_parts])


async def _run_tool_batch(
    message: Message,
    stop_reason: StopReason | None,
    state: LoopState,
    progress: _Progress,
    hooks: LoopHooks,
    max_tool_calls_per_turn: int | None,
) -> AsyncIterator[ToolStarted | ToolProgress | ToolCompleted]:
    """Answer one assistant message's tool calls with what ``answer_tool_calls`` gives, else plan and run them.

    Either way every call is recorded, and its start announced, in call order, and its answer reaches the model in
    that order. A refused call's completion is announced next, since its answer already exists, and each call that
    runs announces its completion as it finishes, so a stop that cuts the batch short finds each finished call
    finished.
    """
    assert message.tool_calls
    answers = None if hooks.answer_tool_calls is None else hooks.answer_tool_calls(message, stop_reason)
    if answers is not None:
        batch = answered_tool_batch(answers)
    else:
        if hooks.rewrite_tool_calls is not None:
            message.tool_calls = hooks.rewrite_tool_calls(message.tool_calls, state.offered.functions)
        batch = plan_tool_batch(
            message.tool_calls,
            state.offered.functions,
            calls_so_far=progress.calls_so_far,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
            entity=_entity(state.record),
        )
    _record_batch(state.record, batch)
    if hooks.before_dispatch is not None:
        await hooks.before_dispatch(state, batch.planned)
    for call in batch.calls:
        yield ToolStarted(call.execution.call)
    for call in batch.calls:
        if isinstance(call, RefusedCall):
            yield ToolCompleted(call.execution.call)
    executed: list[Message] = []
    finished = dispatch_tool_calls(batch.planned, run=state.run, middleware=hooks.tool_middleware)
    async with closing_async_stream(finished):
        async for item in finished:
            if isinstance(item, ToolProgress):
                yield item
                continue
            call, result = item
            executed.append(result)
            yield ToolCompleted(call.execution.call)
    progress.calls_so_far += len(executed) + sum(isinstance(call, RefusedCall) and call.counts for call in batch.calls)
    progress.stopped = await _answer_batch(state.messages, batch.answers(executed), hooks.after_tool_batch)
    # A call of this batch paused, or a tool left a decision on the record (a delegation whose member paused).
    progress.paused = batch.paused or _waits_for_decision(state.record)


async def _answer_resumed(
    decided: Sequence[RunRequirement],
    state: LoopState,
    progress: _Progress,
    hooks: LoopHooks,
) -> AsyncIterator[ToolStarted | ToolProgress | ToolCompleted]:
    """Answer the requirements a resume decided, in order, as the rest of the batch the run paused in.

    Approved calls run from their stored arguments and announce themselves as tool events; denied calls answer with
    their note; external results answer as they are. The run then stops, pauses, or continues as after any batch:
    a member that paused once more leaves the record waiting, so the loop sends no request.
    """
    results: list[Message] = []
    for requirement in decided:
        execution = requirement.tool_execution
        if execution.confirmed is True:
            function = state.offered.functions[execution.tool_name]
            # The decision has been taken: the entry no longer waits, so the dispatcher runs it.
            execution.requires_confirmation = False
            planned = PlannedCall(function=function, execution=execution)
            yield ToolStarted(execution.call)
            finished = dispatch_tool_calls([planned], run=state.run, middleware=hooks.tool_middleware)
            async with closing_async_stream(finished):
                async for item in finished:
                    if isinstance(item, ToolProgress):
                        yield item
                    else:
                        results.append(item[1])
            yield ToolCompleted(execution.call)
        elif execution.confirmed is False:
            execution.is_error = True
            execution.requires_confirmation = False
            results.append(
                failed_answer(
                    execution.tool_call_id,
                    execution.tool_name,
                    requirement.denial_note,
                    tool_args=execution.tool_args,
                    stop_after_tool_call=execution.stop_after_call,
                ),
            )
        if execution.external_execution_required:
            results.append(tool_result_message(execution))
            execution.external_execution_required = False
    progress.stopped = await _answer_batch(state.messages, results, hooks.on_resume_entry)
    progress.paused = _waits_for_decision(state.record)


def _entity(record: AnyRunRecord) -> str | None:
    """Return the agent or team the run belongs to, which its loop's warnings name."""
    return record.agent_id if isinstance(record, RunRecord) else record.team_id


def _waits_for_decision(record: AnyRunRecord) -> bool:
    return any(not requirement.is_resolved() for requirement in record.requirements)


async def _answer_batch(
    messages: list[Message],
    results: Sequence[Message],
    hooks: Sequence[AfterToolBatchHook],
) -> bool:
    """Append one batch's results and return whether the batch stops the run, running ``hooks`` only when it does not.

    Each result is one canonical tool message; media leave them first, so a wire that merges results never carries
    them, and reach the model on one user message after the results. Every batch path answers here, so one rule
    decides the stop: a batch stops the run when any of its results does, counting the results a resumed batch
    answered before its pause. No later request reads what a hook would add after a batch that stops the run, so none
    runs.
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
) -> AsyncIterator[TextDelta | ReasoningDelta | TurnCompleted]:
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
                if isinstance(event, TextDelta | ReasoningDelta):
                    yield event
                elif isinstance(event, UsageReported):
                    reported.observe(event)
                elif isinstance(event, TurnCompleted):
                    turn = event
        if turn is None:
            raise incomplete_stream_error(provider=identity.provider)
        yield turn
        # The consumer asked for more, so it holds the turn and accepts it before its next suspension point.
        handed_off = True
    finally:
        if not handed_off:
            # A cut-off request settles exactly what was billed: the completed turn's usage, else the final reports
            # plus a partial none replaced.
            billed = turn.message.metrics if turn is not None else reported.received
            if billed is not None and billed.reported:
                provider_data = model_provenance(identity.id, identity.provider)
                # The retrying stage marks usage that sums attempts on the finished turn and on the reports alike.
                sums_attempts = (
                    usage_sums_attempts(turn.message.provider_data) if turn is not None else reported.sums_attempts
                )
                if sums_attempts:
                    # Usage that sums retried attempts must not read as one request's.
                    provider_data = {AGGREGATE_USAGE_KEY: True, **provider_data}
                messages.append(Message(role="assistant", metrics=billed, provider_data=provider_data))
                _count_request(metrics, billed, identity)


def _count_request(metrics: RunMetrics, usage: MessageMetrics, identity: ModelIdentity) -> None:
    metrics.add(usage, model_id=identity.id, provider=identity.provider)
    if metrics.time_to_first_token is None:
        metrics.time_to_first_token = usage.time_to_first_token


def _record_batch(record: AnyRunRecord, batch: ToolBatch) -> None:
    """Add every call's execution to the record in call order, refused and paused ones included.

    Each refusal that does not count toward the tool-call limit is counted in ``uncounted_tool_calls``, so a resume
    charges the limit only for the calls that count.
    """
    if record.tools is None:
        record.tools = []
    record.uncounted_tool_calls += sum(isinstance(call, RefusedCall) and not call.counts for call in batch.calls)
    record.tools.extend(call.execution for call in batch.calls)


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
