"""Shared response-turn drivers for the agent and team envelopes.

One inbound response turn has the same lifecycle regardless of which entity
answers it: open the scope session, run prepared attempts (each attempt owns
its own media-fallback retries), decide whether the turn continues after a
dynamic-tool call or a discarded empty run, record the outcome on the turn
recorder, and persist an interrupted replay when the turn is cancelled without
a recorder. This module owns that lifecycle exactly once; ``mindroom.ai`` and
``mindroom.teams`` supply thin adapters carrying the entity-specific attempt
bodies as injected callables.

Every ``ResponseTurnContext`` field is consumed by the drivers themselves;
state that only the attempt bodies need stays captured inside the adapter
closures and never crosses this seam.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, NoReturn, cast
from uuid import uuid4

from mindroom import ai_runtime
from mindroom.ai_turn_state import AITurnState
from mindroom.cancellation import build_cancelled_error
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
)
from mindroom.dynamic_tool_continuation import DYNAMIC_TOOL_CONTINUATION_LIMIT, continuation_decision_from_tools
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
    from contextlib import AbstractContextManager

    from agno.models.response import ToolExecution

    from mindroom.history import ScopeSessionContext
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

__all__ = [
    "AttemptResolved",
    "BlockingAttemptResolution",
    "BlockingTurnAdapter",
    "CancelledAttempt",
    "CompletedAttempt",
    "ContinuationCapability",
    "DynamicContinuationRunState",
    "EmptyRunCapability",
    "EmptyRunDiscard",
    "ErroredAttempt",
    "HandledAttempt",
    "ResponseTurnContext",
    "StandaloneReplaySnapshot",
    "StreamAttemptResolution",
    "StreamingTurnAdapter",
    "TurnPartialSnapshot",
    "TurnRunState",
    "TurnSinks",
    "build_matrix_run_metadata",
    "run_blocking_response_turn",
    "stream_response_turn",
]


def _normalized_string_list(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in normalized:
            normalized.append(value)
    return normalized


def build_matrix_run_metadata(
    reply_to_event_id: str | None,
    unseen_event_ids: list[str],
    *,
    room_id: str | None = None,
    thread_id: str | None = None,
    requester_id: str | None = None,
    correlation_id: str | None = None,
    tools_schema: list[dict[str, object]] | None = None,
    model_params: dict[str, Any] | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build metadata dict for a run, tracking consumed Matrix event ids."""
    metadata = dict(extra_metadata or {})
    if room_id is not None:
        metadata["room_id"] = room_id
    if thread_id is not None:
        metadata["thread_id"] = thread_id
    if reply_to_event_id is not None:
        metadata["reply_to_event_id"] = reply_to_event_id
    if requester_id is not None:
        metadata["requester_id"] = requester_id
    if correlation_id is not None:
        metadata["correlation_id"] = correlation_id
    if tools_schema is not None:
        metadata["tools_schema"] = tools_schema
    else:
        metadata.setdefault("tools_schema", [])
    if model_params is not None:
        metadata["model_params"] = model_params
    else:
        metadata.setdefault("model_params", {})
    source_event_ids = _normalized_string_list(metadata.get(MATRIX_SOURCE_EVENT_IDS_METADATA_KEY))
    if reply_to_event_id:
        seen_event_ids = _normalized_string_list(
            [
                reply_to_event_id,
                *source_event_ids,
                *_normalized_string_list(metadata.get(MATRIX_SEEN_EVENT_IDS_METADATA_KEY)),
                *unseen_event_ids,
            ],
        )
        metadata[MATRIX_EVENT_ID_METADATA_KEY] = reply_to_event_id
        metadata[MATRIX_SEEN_EVENT_IDS_METADATA_KEY] = seen_event_ids
    if MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY in metadata and not isinstance(
        metadata[MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY],
        dict,
    ):
        metadata.pop(MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY, None)
    return metadata or None


@dataclass(frozen=True)
class DynamicContinuationRunState:
    """Prompt and run identity for a dynamic-tool continuation sequence."""

    original_prompt: str
    active_prompt: str
    active_model_prompt: str | None
    active_current_timestamp_ms: float | None
    active_current_prompt_is_structured: bool
    active_run_id: str | None
    continuation_model_prompt_tail: str

    @classmethod
    def initial(
        cls,
        *,
        prompt: str,
        model_prompt: str | None,
        current_timestamp_ms: float | None,
        current_prompt_is_structured: bool,
        run_id: str | None,
        continuation_model_prompt_tail: str,
    ) -> DynamicContinuationRunState:
        """Build the continuation state for one turn's first attempt."""
        return cls(
            original_prompt=prompt,
            active_prompt=prompt,
            active_model_prompt=model_prompt,
            active_current_timestamp_ms=current_timestamp_ms,
            active_current_prompt_is_structured=current_prompt_is_structured,
            active_run_id=run_id,
            continuation_model_prompt_tail=continuation_model_prompt_tail,
        )

    def advance(
        self,
        *,
        continuation_prompt: str,
        previous_run_id: str | None,
    ) -> DynamicContinuationRunState:
        """Return the continuation state for one more same-turn attempt."""
        return replace(
            self,
            active_prompt=continuation_prompt,
            active_model_prompt=self.continuation_model_prompt_tail or None,
            active_current_timestamp_ms=None,
            active_current_prompt_is_structured=False,
            active_run_id=ai_runtime.next_retry_run_id(previous_run_id),
        )


@dataclass(frozen=True)
class ResponseTurnContext:
    """Per-turn Matrix identity constants consumed by the turn drivers."""

    entity_label: str
    session_id: str | None
    run_id: str | None
    correlation_id: str
    reply_to_event_id: str | None
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    matrix_run_metadata: dict[str, Any] | None


@dataclass(frozen=True)
class TurnSinks:
    """Mutable turn outputs owned by the caller and updated by the drivers."""

    turn_recorder: TurnRecorder | None = None
    run_metadata_collector: dict[str, Any] | None = None


@dataclass
class TurnRunState:
    """Driver-owned mutable state shared with the adapter attempt bodies.

    Attempt bodies must set ``run_metadata`` and ``unseen_event_ids`` once
    prepare finishes; interruption recording falls back to rebuilt metadata
    while they are still unset.
    """

    turn_state: AITurnState = field(default_factory=AITurnState)
    scope_context: ScopeSessionContext | None = None
    run_metadata: dict[str, Any] | None = None
    unseen_event_ids: list[str] = field(default_factory=list)
    standalone_replay_persisted: bool = False
    empty_response_retried: bool = False


@dataclass(frozen=True)
class TurnPartialSnapshot:
    """Live partial-output view used when a turn is cancelled from outside."""

    assistant_text: str = ""
    completed_tools: tuple[ToolTraceEntry, ...] = ()
    interrupted_tools: tuple[ToolTraceEntry, ...] = ()
    attempt_run_id: str | None = None


@dataclass(frozen=True)
class CompletedAttempt:
    """One attempt that ran to a terminal provider response."""

    # Only blocking attempts set this; the streaming driver delivers via
    # chunks and records replayable_text.
    response_text: str = ""
    replayable_text: str = ""
    has_visible_content: bool = False
    is_empty: bool = False
    session_id: str | None = None
    run_id: str | None = None
    attempt_run_id: str | None = None
    output_tokens: int | None = None
    tool_executions: tuple[ToolExecution, ...] = ()
    completed_tools: tuple[ToolTraceEntry, ...] = ()
    metadata_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class CancelledAttempt:
    """One attempt whose provider run reported cancellation."""

    reason: str | None = None
    partial_text: str = ""
    completed_tools: tuple[ToolTraceEntry, ...] = ()
    interrupted_tools: tuple[ToolTraceEntry, ...] = ()
    session_id: str | None = None
    run_id: str | None = None
    metadata_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class ErroredAttempt:
    """One attempt that resolved to user-facing error text (blocking only)."""

    user_message_text: str
    metadata_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class HandledAttempt:
    """One streaming attempt that already emitted and recorded its own terminal output."""


BlockingAttemptResolution = CompletedAttempt | CancelledAttempt | ErroredAttempt
StreamAttemptResolution = CompletedAttempt | CancelledAttempt | HandledAttempt


@dataclass(frozen=True)
class AttemptResolved:
    """Sentinel a streaming attempt yields last to report its resolution."""

    resolution: StreamAttemptResolution


@dataclass(frozen=True)
class ContinuationCapability:
    """Enable dynamic-tool continuations for the turn.

    The iteration budget is always ``DYNAMIC_TOOL_CONTINUATION_LIMIT``:
    ``continuation_decision_from_tools`` hardcodes that constant for its
    continue/stop decision, so a per-adapter limit would silently desync
    from the loop budget.
    """


@dataclass(frozen=True)
class EmptyRunDiscard:
    """Identity of one empty completed run to purge from session history."""

    session_id: str | None
    run_id: str | None
    output_tokens: int | None


@dataclass(frozen=True)
class EmptyRunCapability:
    """Enable the empty-completed-run guard with an entity-specific discard."""

    notice_text: str
    discard: Callable[[ScopeSessionContext | None, EmptyRunDiscard], None]


@dataclass(frozen=True)
class StandaloneReplaySnapshot:
    """Interrupted-turn state persisted when no turn recorder is attached."""

    session_id: str | None
    run_id: str
    partial_text: str
    completed_tools: list[ToolTraceEntry]
    interrupted_tools: list[ToolTraceEntry]
    run_metadata: dict[str, Any] | None


@dataclass(frozen=True)
class BlockingTurnAdapter:
    """Entity-specific callbacks for one blocking response turn."""

    open_scope: Callable[[], AbstractContextManager[ScopeSessionContext | None]]
    run_attempt: Callable[
        [TurnRunState, DynamicContinuationRunState],
        Awaitable[BlockingAttemptResolution],
    ]
    snapshot_partial: Callable[[], TurnPartialSnapshot]
    release_attempt_entity: Callable[[ScopeSessionContext | None], None]
    close_runtime_dbs: Callable[[ScopeSessionContext | None], None]
    on_scope_opened: Callable[[ScopeSessionContext | None], None] | None = None
    finalize_attempt: Callable[[ScopeSessionContext | None], None] | None = None
    unexpected_error_text: Callable[[Exception], str] | None = None
    continuation: ContinuationCapability | None = None
    empty_run: EmptyRunCapability | None = None
    persist_standalone_replay: Callable[[ScopeSessionContext | None, StandaloneReplaySnapshot], None] | None = None


@dataclass(frozen=True)
class StreamingTurnAdapter[ChunkT]:
    """Entity-specific callbacks for one streaming response turn."""

    open_scope: Callable[[], AbstractContextManager[ScopeSessionContext | None]]
    run_attempt: Callable[
        [TurnRunState, DynamicContinuationRunState],
        AsyncIterator[ChunkT | AttemptResolved],
    ]
    snapshot_partial: Callable[[], TurnPartialSnapshot]
    release_attempt_entity: Callable[[ScopeSessionContext | None], None]
    close_runtime_dbs: Callable[[ScopeSessionContext | None], None]
    on_scope_opened: Callable[[ScopeSessionContext | None], None] | None = None
    finalize_attempt: Callable[[ScopeSessionContext | None], None] | None = None
    make_notice_chunk: Callable[[str], ChunkT] | None = None
    unexpected_error_text: Callable[[Exception], str] | None = None
    continuation: ContinuationCapability | None = None
    empty_run: EmptyRunCapability | None = None
    persist_standalone_replay: Callable[[ScopeSessionContext | None, StandaloneReplaySnapshot], None] | None = None


def _raise_continuation_budget_exhausted() -> NoReturn:
    # The continuation loop always settles on its final iteration: at the limit
    # the decision carries a limit_message and never asks to continue.
    msg = "dynamic tool continuation loop must return within its iteration budget"
    raise AssertionError(msg)


def _raise_missing_stream_resolution(entity_label: str) -> NoReturn:
    # A streaming attempt that returns without its sentinel would otherwise end
    # the turn silently: nothing recorded, no metadata published, no log.
    msg = f"streaming attempt for {entity_label!r} ended without yielding its AttemptResolved sentinel"
    raise RuntimeError(msg)


def _effective_continuation_limit(
    continuation: ContinuationCapability | None,
    empty_run: EmptyRunCapability | None,
) -> int:
    """Return the turn's extra-iteration budget beyond the first attempt.

    The empty-run one-shot retry borrows a continuation slot when continuations
    are enabled; without them it still needs one slot of its own.
    """
    if continuation is not None:
        return DYNAMIC_TOOL_CONTINUATION_LIMIT
    return 1 if empty_run is not None else 0


def _reset_turn_state_for_dynamic_continuation(
    *,
    turn_recorder: TurnRecorder | None,
    run_metadata: dict[str, Any] | None,
    completed_tools_for_turn: list[ToolTraceEntry],
) -> AITurnState:
    turn_state = AITurnState(prior_completed_tools=completed_tools_for_turn)
    turn_state.sync_partial(
        turn_recorder,
        run_metadata=run_metadata,
        assistant_text="",
        completed_tools=[],
        interrupted_tools=[],
    )
    return turn_state


def _fallback_matrix_run_metadata(ctx: ResponseTurnContext, run: TurnRunState) -> dict[str, Any] | None:
    """Build run metadata for interruptions that fire before prepare finished."""
    return build_matrix_run_metadata(
        ctx.reply_to_event_id,
        run.unseen_event_ids,
        room_id=ctx.room_id,
        thread_id=ctx.thread_id,
        requester_id=ctx.requester_id,
        correlation_id=ctx.correlation_id,
        extra_metadata=deepcopy(ctx.matrix_run_metadata),
    )


def _persist_attempt_cancelled_replay(
    ctx: ResponseTurnContext,
    persist: Callable[[ScopeSessionContext | None, StandaloneReplaySnapshot], None],
    run: TurnRunState,
    resolution: CancelledAttempt,
) -> None:
    """Persist the standalone interrupted replay for one cancelled attempt."""
    persist(
        run.scope_context,
        StandaloneReplaySnapshot(
            session_id=resolution.session_id or ctx.session_id,
            run_id=resolution.run_id or str(uuid4()),
            partial_text=resolution.partial_text,
            completed_tools=run.turn_state.completed_tools_for(resolution.completed_tools),
            interrupted_tools=list(resolution.interrupted_tools),
            run_metadata=run.run_metadata,
        ),
    )
    run.standalone_replay_persisted = True


def _record_turn_cancelled_fallback(
    ctx: ResponseTurnContext,
    persist: Callable[[ScopeSessionContext | None, StandaloneReplaySnapshot], None] | None,
    sinks: TurnSinks,
    run: TurnRunState,
    snapshot: TurnPartialSnapshot,
    *,
    use_recorder_state: bool,
) -> None:
    """Record an externally cancelled turn on the recorder or as a standalone replay."""
    recorder = sinks.turn_recorder
    if recorder is not None:
        run_metadata = (
            run.run_metadata
            if run.run_metadata is not None
            else recorder.run_metadata or _fallback_matrix_run_metadata(ctx, run)
        )
        if use_recorder_state:
            run.turn_state.record_interrupted_from_recorder(recorder, run_metadata=run_metadata)
        else:
            run.turn_state.record_interrupted(
                recorder,
                run_metadata=run_metadata,
                assistant_text=snapshot.assistant_text,
                completed_tools=list(snapshot.completed_tools),
                interrupted_tools=list(snapshot.interrupted_tools),
            )
        return
    if run.standalone_replay_persisted or persist is None:
        return
    persist(
        run.scope_context,
        StandaloneReplaySnapshot(
            session_id=ctx.session_id,
            run_id=(snapshot.attempt_run_id or ctx.run_id) or str(uuid4()),
            partial_text=snapshot.assistant_text,
            completed_tools=run.turn_state.completed_tools_for(snapshot.completed_tools),
            interrupted_tools=list(snapshot.interrupted_tools),
            run_metadata=run.run_metadata if run.run_metadata is not None else _fallback_matrix_run_metadata(ctx, run),
        ),
    )


@dataclass(frozen=True)
class _EmptyRunOutcome:
    """How the empty-run guard settled one empty completed attempt."""

    retry_granted: bool
    notice_text: str | None = None


def _settle_empty_run(
    ctx: ResponseTurnContext,
    empty_run: EmptyRunCapability,
    release_attempt_entity: Callable[[ScopeSessionContext | None], None],
    run: TurnRunState,
    resolution: CompletedAttempt,
    *,
    continuation_count: int,
    limit: int,
) -> _EmptyRunOutcome:
    """Discard one empty completed run and decide whether one retry is granted.

    The one-shot retry borrows a continuation slot so the outer loop's
    iteration budget stays authoritative; a granted retry closes the spent
    entity's runtime state exactly like the continuation handoff.
    """
    empty_run.discard(
        run.scope_context,
        EmptyRunDiscard(
            session_id=resolution.session_id or ctx.session_id,
            run_id=resolution.run_id,
            output_tokens=resolution.output_tokens,
        ),
    )
    if not run.empty_response_retried and continuation_count < limit:
        run.empty_response_retried = True
        release_attempt_entity(run.scope_context)
        return _EmptyRunOutcome(retry_granted=True)
    return _EmptyRunOutcome(retry_granted=False, notice_text=empty_run.notice_text)


def _advance_turn_continuation(
    sinks: TurnSinks,
    release_attempt_entity: Callable[[ScopeSessionContext | None], None],
    run: TurnRunState,
    resolution: CompletedAttempt,
    continuation: DynamicContinuationRunState,
    *,
    next_prompt: str | None,
) -> DynamicContinuationRunState:
    """Close the spent attempt entity and prepare run state for one more continuation."""
    completed_tools_for_turn = run.turn_state.completed_tools_for(resolution.completed_tools)
    release_attempt_entity(run.scope_context)
    advanced = continuation.advance(
        continuation_prompt=next_prompt or continuation.original_prompt,
        previous_run_id=resolution.attempt_run_id,
    )
    run.turn_state = _reset_turn_state_for_dynamic_continuation(
        turn_recorder=sinks.turn_recorder,
        run_metadata=run.run_metadata,
        completed_tools_for_turn=completed_tools_for_turn,
    )
    return advanced


async def run_blocking_response_turn(
    ctx: ResponseTurnContext,
    adapter: BlockingTurnAdapter,
    sinks: TurnSinks,
    *,
    continuation: DynamicContinuationRunState,
) -> str:
    """Run one blocking response turn to a final user-visible text."""
    run = TurnRunState()
    limit = _effective_continuation_limit(adapter.continuation, adapter.empty_run)
    try:
        with adapter.open_scope() as scope_context:
            run.scope_context = scope_context
            if adapter.on_scope_opened is not None:
                adapter.on_scope_opened(scope_context)
            for continuation_count in range(limit + 1):
                try:
                    resolution = await adapter.run_attempt(run, continuation)
                    settled = _settle_blocking_attempt(
                        ctx,
                        adapter,
                        sinks,
                        run,
                        resolution,
                        continuation,
                        continuation_count=continuation_count,
                        limit=limit,
                    )
                finally:
                    if adapter.finalize_attempt is not None:
                        adapter.finalize_attempt(run.scope_context)
                if isinstance(settled, str):
                    return settled
                continuation = settled
            _raise_continuation_budget_exhausted()
    except asyncio.CancelledError:
        # The blocking envelope re-records from the recorder's canonical state so
        # an in-attempt cancellation (recorded above with attempt-local partials)
        # and an external task cancel converge on the same interrupted turn.
        _record_turn_cancelled_fallback(
            ctx,
            adapter.persist_standalone_replay,
            sinks,
            run,
            adapter.snapshot_partial(),
            use_recorder_state=True,
        )
        raise
    except Exception as e:
        if adapter.unexpected_error_text is None:
            raise
        logger.exception("Response turn failed", entity=ctx.entity_label)
        return adapter.unexpected_error_text(e)
    finally:
        adapter.close_runtime_dbs(run.scope_context)


def _settle_blocking_attempt(
    ctx: ResponseTurnContext,
    adapter: BlockingTurnAdapter,
    sinks: TurnSinks,
    run: TurnRunState,
    resolution: BlockingAttemptResolution,
    continuation: DynamicContinuationRunState,
    *,
    continuation_count: int,
    limit: int,
) -> str | DynamicContinuationRunState:
    """Settle one blocking attempt: return final text or the advanced continuation."""
    # The blocking envelope publishes run metadata before dispatching on the
    # attempt outcome, so cancelled and errored runs still reach the collector.
    if sinks.run_metadata_collector is not None and resolution.metadata_content is not None:
        sinks.run_metadata_collector.update(resolution.metadata_content)
    if isinstance(resolution, CancelledAttempt):
        run.turn_state.record_interrupted(
            sinks.turn_recorder,
            run_metadata=run.run_metadata,
            assistant_text=resolution.partial_text,
            completed_tools=list(resolution.completed_tools),
            interrupted_tools=list(resolution.interrupted_tools),
        )
        if sinks.turn_recorder is None and adapter.persist_standalone_replay is not None:
            _persist_attempt_cancelled_replay(ctx, adapter.persist_standalone_replay, run, resolution)
        raise build_cancelled_error(resolution.reason)
    if isinstance(resolution, ErroredAttempt):
        return resolution.user_message_text
    return _settle_completed_blocking_attempt(
        ctx,
        adapter,
        sinks,
        run,
        resolution,
        continuation,
        continuation_count=continuation_count,
        limit=limit,
    )


def _settle_completed_blocking_attempt(
    ctx: ResponseTurnContext,
    adapter: BlockingTurnAdapter,
    sinks: TurnSinks,
    run: TurnRunState,
    resolution: CompletedAttempt,
    continuation: DynamicContinuationRunState,
    *,
    continuation_count: int,
    limit: int,
) -> str | DynamicContinuationRunState:
    """Settle one completed blocking attempt into final text or a continuation."""
    if resolution.is_empty and adapter.empty_run is not None:
        empty_outcome = _settle_empty_run(
            ctx,
            adapter.empty_run,
            adapter.release_attempt_entity,
            run,
            resolution,
            continuation_count=continuation_count,
            limit=limit,
        )
        if empty_outcome.retry_granted:
            return continuation
        run.turn_state.record_completed(
            sinks.turn_recorder,
            run_metadata=run.run_metadata,
            assistant_text="",
            completed_tools=[],
        )
        assert empty_outcome.notice_text is not None
        return empty_outcome.notice_text
    decision = (
        continuation_decision_from_tools(
            resolution.tool_executions,
            original_prompt=continuation.original_prompt,
            continuation_count=continuation_count,
        )
        if adapter.continuation is not None
        else None
    )
    response_text = resolution.response_text
    replayable_text = resolution.replayable_text
    if decision is not None:
        if decision.should_continue:
            return _advance_turn_continuation(
                sinks,
                adapter.release_attempt_entity,
                run,
                resolution,
                continuation,
                next_prompt=decision.next_prompt,
            )
        if decision.limit_message is not None and decision.continuation is not None:
            logger.warning(
                "Dynamic tool continuation limit reached",
                entity=ctx.entity_label,
                function_name=decision.continuation.function_name,
                tool_name=decision.continuation.tool_name,
                status=decision.continuation.status,
            )
        if decision.limit_message is not None and not resolution.has_visible_content:
            response_text = decision.limit_message
            replayable_text = decision.limit_message
    run.turn_state.record_completed(
        sinks.turn_recorder,
        run_metadata=run.run_metadata,
        assistant_text=replayable_text,
        completed_tools=list(resolution.completed_tools),
    )
    return response_text


@dataclass(frozen=True)
class _StreamCompletionSettle:
    """Driver-internal plan for finishing one completed streaming attempt."""

    keep_going: bool
    continuation: DynamicContinuationRunState
    notice_texts: tuple[str, ...]
    recorded_text: str


def _settle_completed_stream_attempt(
    ctx: ResponseTurnContext,
    adapter: StreamingTurnAdapter[Any],
    sinks: TurnSinks,
    run: TurnRunState,
    resolution: CompletedAttempt,
    continuation: DynamicContinuationRunState,
    *,
    continuation_count: int,
    limit: int,
) -> _StreamCompletionSettle:
    """Settle one completed streaming attempt into notices, recording text, and continuation."""
    notice_texts: list[str] = []
    keep_going = False
    recorded_text = resolution.replayable_text
    if resolution.is_empty and adapter.empty_run is not None:
        empty_outcome = _settle_empty_run(
            ctx,
            adapter.empty_run,
            adapter.release_attempt_entity,
            run,
            resolution,
            continuation_count=continuation_count,
            limit=limit,
        )
        if empty_outcome.retry_granted:
            keep_going = True
        else:
            # The notice falls through: run metadata and recorder completion
            # still apply to this notice-only turn.
            assert empty_outcome.notice_text is not None
            notice_texts.append(empty_outcome.notice_text)
    if not keep_going and adapter.continuation is not None:
        decision = continuation_decision_from_tools(
            resolution.tool_executions,
            original_prompt=continuation.original_prompt,
            continuation_count=continuation_count,
        )
        if decision.should_continue:
            continuation = _advance_turn_continuation(
                sinks,
                adapter.release_attempt_entity,
                run,
                resolution,
                continuation,
                next_prompt=decision.next_prompt,
            )
            keep_going = True
        elif decision.limit_message is not None:
            if decision.continuation is not None:
                logger.warning(
                    "Dynamic tool continuation limit reached during streaming",
                    entity=ctx.entity_label,
                    function_name=decision.continuation.function_name,
                    tool_name=decision.continuation.tool_name,
                    status=decision.continuation.status,
                )
            if not resolution.has_visible_content:
                notice_texts.append(decision.limit_message)
                recorded_text = decision.limit_message
    return _StreamCompletionSettle(
        keep_going=keep_going,
        continuation=continuation,
        notice_texts=tuple(notice_texts),
        recorded_text=recorded_text,
    )


async def stream_response_turn[ChunkT](  # noqa: C901, PLR0912, PLR0915
    ctx: ResponseTurnContext,
    adapter: StreamingTurnAdapter[ChunkT],
    sinks: TurnSinks,
    *,
    continuation: DynamicContinuationRunState,
) -> AsyncGenerator[ChunkT, None]:
    """Run one streaming response turn, yielding the attempt chunks as they arrive."""
    run = TurnRunState()
    limit = _effective_continuation_limit(adapter.continuation, adapter.empty_run)
    try:
        with adapter.open_scope() as scope_context:
            run.scope_context = scope_context
            if adapter.on_scope_opened is not None:
                adapter.on_scope_opened(scope_context)
            for continuation_count in range(limit + 1):
                resolution: StreamAttemptResolution | None = None
                keep_going = False
                try:
                    async for item in adapter.run_attempt(run, continuation):
                        if isinstance(item, AttemptResolved):
                            # The sentinel must be the attempt's final yield; never
                            # break out of this loop, so attempt cleanup stays
                            # deterministic at generator return.
                            resolution = item.resolution
                            continue
                        yield item
                    if resolution is None:
                        _raise_missing_stream_resolution(ctx.entity_label)
                    if isinstance(resolution, HandledAttempt):
                        return
                    if isinstance(resolution, CancelledAttempt):
                        # The streaming envelope records the interruption before
                        # publishing cancelled run metadata to the collector.
                        run.turn_state.record_interrupted(
                            sinks.turn_recorder,
                            run_metadata=run.run_metadata,
                            assistant_text=resolution.partial_text,
                            completed_tools=list(resolution.completed_tools),
                            interrupted_tools=list(resolution.interrupted_tools),
                        )
                        if sinks.run_metadata_collector is not None and resolution.metadata_content is not None:
                            sinks.run_metadata_collector.update(resolution.metadata_content)
                        if sinks.turn_recorder is None and adapter.persist_standalone_replay is not None:
                            _persist_attempt_cancelled_replay(
                                ctx,
                                adapter.persist_standalone_replay,
                                run,
                                resolution,
                            )
                        raise build_cancelled_error(resolution.reason)
                    settle = _settle_completed_stream_attempt(
                        ctx,
                        adapter,
                        sinks,
                        run,
                        resolution,
                        continuation,
                        continuation_count=continuation_count,
                        limit=limit,
                    )
                    continuation = settle.continuation
                    keep_going = settle.keep_going
                    for notice_text in settle.notice_texts:
                        assert adapter.make_notice_chunk is not None
                        yield adapter.make_notice_chunk(notice_text)
                    if not keep_going:
                        if sinks.run_metadata_collector is not None and resolution.metadata_content is not None:
                            sinks.run_metadata_collector.update(resolution.metadata_content)
                        run.turn_state.record_completed(
                            sinks.turn_recorder,
                            run_metadata=run.run_metadata,
                            assistant_text=settle.recorded_text,
                            completed_tools=list(resolution.completed_tools),
                        )
                finally:
                    if adapter.finalize_attempt is not None:
                        adapter.finalize_attempt(run.scope_context)
                if not keep_going:
                    return
            _raise_continuation_budget_exhausted()
    except asyncio.CancelledError:
        _record_turn_cancelled_fallback(
            ctx,
            adapter.persist_standalone_replay,
            sinks,
            run,
            adapter.snapshot_partial(),
            use_recorder_state=False,
        )
        raise
    except Exception as e:
        if adapter.unexpected_error_text is None:
            raise
        logger.exception("Response turn failed", entity=ctx.entity_label)
        yield cast("ChunkT", adapter.unexpected_error_text(e))
        return
    finally:
        adapter.close_runtime_dbs(run.scope_context)
