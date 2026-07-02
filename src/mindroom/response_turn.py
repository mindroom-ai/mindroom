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
from typing import TYPE_CHECKING, Any, NoReturn
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
    "DynamicContinuationRunState",
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

    # The attempt's deliverable final text. Streaming attempts whose chunks
    # already delivered the document leave it empty; otherwise the driver
    # emits it only after the attempt settles (an in-attempt yield would leak
    # text that an empty-run retry or continuation is about to supersede).
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
class EmptyRunDiscard:
    """Identity of one empty completed run to purge from session history."""

    session_id: str | None
    run_id: str | None
    output_tokens: int | None


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
    discard_empty_run: Callable[[ScopeSessionContext | None, EmptyRunDiscard], None]
    on_scope_opened: Callable[[ScopeSessionContext | None], None] | None = None
    finalize_attempt: Callable[[ScopeSessionContext | None], None] | None = None
    unexpected_error_text: Callable[[Exception], str] | None = None
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
    discard_empty_run: Callable[[ScopeSessionContext | None, EmptyRunDiscard], None]
    make_text_chunk: Callable[[str], ChunkT]
    on_scope_opened: Callable[[ScopeSessionContext | None], None] | None = None
    finalize_attempt: Callable[[ScopeSessionContext | None], None] | None = None
    unexpected_error_text: Callable[[Exception], str] | None = None
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


def _settle_empty_run(
    ctx: ResponseTurnContext,
    discard_empty_run: Callable[[ScopeSessionContext | None, EmptyRunDiscard], None],
    release_attempt_entity: Callable[[ScopeSessionContext | None], None],
    run: TurnRunState,
    resolution: CompletedAttempt,
    *,
    continuation_count: int,
) -> bool:
    """Discard one empty completed run; return whether one retry is granted.

    The one-shot retry borrows a continuation slot so the outer loop's
    iteration budget stays authoritative; a granted retry closes the spent
    entity's runtime state exactly like the continuation handoff.
    """
    discard_empty_run(
        run.scope_context,
        EmptyRunDiscard(
            session_id=resolution.session_id or ctx.session_id,
            run_id=resolution.run_id,
            output_tokens=resolution.output_tokens,
        ),
    )
    if not run.empty_response_retried and continuation_count < DYNAMIC_TOOL_CONTINUATION_LIMIT:
        run.empty_response_retried = True
        release_attempt_entity(run.scope_context)
        return True
    return False


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
    try:
        with adapter.open_scope() as scope_context:
            run.scope_context = scope_context
            if adapter.on_scope_opened is not None:
                adapter.on_scope_opened(scope_context)
            for continuation_count in range(DYNAMIC_TOOL_CONTINUATION_LIMIT + 1):
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
    settle = _settle_completed_attempt(
        ctx,
        sinks,
        run,
        resolution,
        continuation,
        discard_empty_run=adapter.discard_empty_run,
        release_attempt_entity=adapter.release_attempt_entity,
        continuation_count=continuation_count,
    )
    if settle.keep_going:
        return settle.continuation
    run.turn_state.record_completed(
        sinks.turn_recorder,
        run_metadata=run.run_metadata,
        assistant_text=settle.recorded_text,
        completed_tools=list(settle.recorded_tools),
    )
    return settle.response_text


@dataclass(frozen=True)
class _CompletionSettle:
    """Driver-internal plan for finishing one completed attempt."""

    keep_going: bool
    continuation: DynamicContinuationRunState
    # The streaming driver emits these as chunks; the blocking driver returns
    # response_text. Both cover the same notice/limit/final-text decisions.
    deliver_texts: tuple[str, ...]
    recorded_text: str
    recorded_tools: tuple[ToolTraceEntry, ...]
    response_text: str


def _settle_completed_attempt(
    ctx: ResponseTurnContext,
    sinks: TurnSinks,
    run: TurnRunState,
    resolution: CompletedAttempt,
    continuation: DynamicContinuationRunState,
    *,
    discard_empty_run: Callable[[ScopeSessionContext | None, EmptyRunDiscard], None],
    release_attempt_entity: Callable[[ScopeSessionContext | None], None],
    continuation_count: int,
) -> _CompletionSettle:
    """Settle one completed attempt into a record/deliver plan or a continuation."""
    if resolution.is_empty:
        retry_granted = _settle_empty_run(
            ctx,
            discard_empty_run,
            release_attempt_entity,
            run,
            resolution,
            continuation_count=continuation_count,
        )
        if retry_granted:
            return _CompletionSettle(
                keep_going=True,
                continuation=continuation,
                deliver_texts=(),
                recorded_text="",
                recorded_tools=(),
                response_text="",
            )
        # The notice falls through: run metadata and recorder completion
        # still apply to this notice-only turn.
        return _CompletionSettle(
            keep_going=False,
            continuation=continuation,
            deliver_texts=(ai_runtime.EMPTY_RESPONSE_NOTICE,),
            recorded_text="",
            recorded_tools=(),
            response_text=ai_runtime.EMPTY_RESPONSE_NOTICE,
        )
    decision = continuation_decision_from_tools(
        resolution.tool_executions,
        original_prompt=continuation.original_prompt,
        continuation_count=continuation_count,
    )
    if decision.should_continue:
        return _CompletionSettle(
            keep_going=True,
            continuation=_advance_turn_continuation(
                sinks,
                release_attempt_entity,
                run,
                resolution,
                continuation,
                next_prompt=decision.next_prompt,
            ),
            deliver_texts=(),
            recorded_text="",
            recorded_tools=(),
            response_text="",
        )
    deliver_texts: tuple[str, ...] = (resolution.response_text,) if resolution.response_text else ()
    recorded_text = resolution.replayable_text
    response_text = resolution.response_text
    if decision.limit_message is not None:
        if decision.continuation is not None:
            logger.warning(
                "Dynamic tool continuation limit reached",
                entity=ctx.entity_label,
                function_name=decision.continuation.function_name,
                tool_name=decision.continuation.tool_name,
                status=decision.continuation.status,
            )
        if not resolution.has_visible_content:
            deliver_texts = (decision.limit_message,)
            recorded_text = decision.limit_message
            response_text = decision.limit_message
    return _CompletionSettle(
        keep_going=False,
        continuation=continuation,
        deliver_texts=deliver_texts,
        recorded_text=recorded_text,
        recorded_tools=resolution.completed_tools,
        response_text=response_text,
    )


async def stream_response_turn[ChunkT](  # noqa: C901, PLR0912
    ctx: ResponseTurnContext,
    adapter: StreamingTurnAdapter[ChunkT],
    sinks: TurnSinks,
    *,
    continuation: DynamicContinuationRunState,
) -> AsyncGenerator[ChunkT, None]:
    """Run one streaming response turn, yielding the attempt chunks as they arrive."""
    run = TurnRunState()
    try:
        with adapter.open_scope() as scope_context:
            run.scope_context = scope_context
            if adapter.on_scope_opened is not None:
                adapter.on_scope_opened(scope_context)
            for continuation_count in range(DYNAMIC_TOOL_CONTINUATION_LIMIT + 1):
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
                    settle = _settle_completed_attempt(
                        ctx,
                        sinks,
                        run,
                        resolution,
                        continuation,
                        discard_empty_run=adapter.discard_empty_run,
                        release_attempt_entity=adapter.release_attempt_entity,
                        continuation_count=continuation_count,
                    )
                    continuation = settle.continuation
                    keep_going = settle.keep_going
                    for deliver_text in settle.deliver_texts:
                        yield adapter.make_text_chunk(deliver_text)
                    if not keep_going:
                        if sinks.run_metadata_collector is not None and resolution.metadata_content is not None:
                            sinks.run_metadata_collector.update(resolution.metadata_content)
                        run.turn_state.record_completed(
                            sinks.turn_recorder,
                            run_metadata=run.run_metadata,
                            assistant_text=settle.recorded_text,
                            completed_tools=list(settle.recorded_tools),
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
        yield adapter.make_text_chunk(adapter.unexpected_error_text(e))
        return
    finally:
        adapter.close_runtime_dbs(run.scope_context)
