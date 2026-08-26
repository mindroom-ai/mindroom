"""Shared response-turn drivers for the agent and team envelopes.

One inbound response turn has the same lifecycle regardless of which entity
answers it: open the scope session, run prepared attempts, decide whether the
turn continues after a dynamic-tool call or a discarded empty run, record the
outcome on the turn recorder, and persist an interrupted replay when the turn
is cancelled without a recorder.
This module owns that lifecycle exactly once; ``mindroom.ai`` and
``mindroom.teams`` supply thin adapters carrying the entity-specific attempt
bodies as injected callables.

``ResponseTurnContext`` is the per-turn identity carrier built once by the
caller that owns the turn: the drivers consume its Matrix-identity fields,
while ``active_event_ids`` and enrichment items ride along for the entity
prepare chains. Mutable per-turn state (collectors, recorders, callbacks) stays
out of it and crosses via ``TurnSinks`` or the adapters.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import uuid4

from agno.run.base import RunStatus

from mindroom import ai_runtime
from mindroom.ai_turn_state import AITurnState
from mindroom.cancellation import build_cancelled_error
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
    MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY,
)
from mindroom.dynamic_tool_continuation import DYNAMIC_TOOL_CONTINUATION_LIMIT, continuation_decision_from_tools
from mindroom.logging_config import get_logger
from mindroom.streaming import StreamingLifecycleSuspensionError, StreamingPresentation

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from agno.models.response import ToolExecution
    from agno.run.agent import RunOutput, RunPausedEvent
    from agno.run.requirement import RunRequirement
    from agno.run.team import RunPausedEvent as TeamRunPausedEvent
    from agno.run.team import TeamRunOutput

    from mindroom.dispatch_source import ScheduledHistoryBudget
    from mindroom.history.runtime import ScopeSessionContext
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.hooks import EnrichmentItem
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

__all__ = [
    "AttemptResolved",
    "BlockingAttemptResolution",
    "BlockingTurnAdapter",
    "CompletedApprovalRun",
    "CompletedAttempt",
    "DynamicContinuationRunState",
    "EmptyRunDiscard",
    "ExcludedAttempt",
    "HandledAttempt",
    "PausedAttempt",
    "ResponsePausedForApproval",
    "ResponseTurnContext",
    "StandaloneReplaySnapshot",
    "StreamAttemptResolution",
    "StreamingTurnAdapter",
    "TurnPartialSnapshot",
    "TurnRunState",
    "TurnSinks",
    "apply_exact_approval_decisions",
    "build_matrix_run_metadata",
    "paused_attempt_from_event",
    "paused_attempt_from_response",
    "run_blocking_response_turn",
    "stream_response_turn",
]


@dataclass(frozen=True, slots=True)
class CompletedApprovalRun:
    """One completed persisted run plus its normal Matrix response metadata."""

    response_text: str
    metadata_content: dict[str, Any]


def _has_unsupported_approval_requirement(requirement: RunRequirement) -> bool:
    """Return whether MindRoom cannot safely resolve this paused requirement."""
    return (
        requirement.needs_user_input
        or requirement.needs_user_feedback
        or requirement.needs_external_execution
        or (not requirement.is_resolved() and not requirement.needs_confirmation)
    )


def apply_exact_approval_decisions(
    requirements: Sequence[RunRequirement],
    *,
    decisions: Mapping[str, bool],
    denial_reasons: Mapping[str, str | None],
) -> list[RunRequirement]:
    """Apply decisions only when they exactly identify one persisted call each."""
    if any(_has_unsupported_approval_requirement(requirement) for requirement in requirements):
        msg = "Paused run contains an unsupported non-confirmation requirement"
        raise RuntimeError(msg)
    pending = [requirement for requirement in requirements if requirement.needs_confirmation]
    call_ids: list[str] = []
    for requirement in pending:
        tool = requirement.tool_execution
        if tool is None or not tool.tool_call_id:
            msg = "Paused tools no longer match the approval continuation"
            raise RuntimeError(msg)
        call_ids.append(tool.tool_call_id)
    decision_ids = set(decisions)
    if len(call_ids) != len(set(call_ids)) or set(call_ids) != decision_ids or set(denial_reasons) != decision_ids:
        msg = "Paused tools no longer match the approval continuation"
        raise RuntimeError(msg)
    for requirement, tool_call_id in zip(pending, call_ids, strict=True):
        if decisions[tool_call_id]:
            requirement.confirm()
        else:
            requirement.reject(denial_reasons[tool_call_id] or "Not approved by requester")
    return pending


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
    discovery_event_ids = _normalized_string_list(metadata.get(MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY))
    if reply_to_event_id:
        seen_event_ids = _normalized_string_list(
            [
                reply_to_event_id,
                *source_event_ids,
                *discovery_event_ids,
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
    active_current_event_id: str | None
    active_run_id: str | None
    continuation_model_prompt_tail: str
    active_model_name: str | None
    apply_model_to_team_members: bool

    @classmethod
    def initial(
        cls,
        *,
        prompt: str,
        model_prompt: str | None,
        current_timestamp_ms: float | None,
        current_prompt_is_structured: bool,
        current_event_id: str | None,
        run_id: str | None,
        continuation_model_prompt_tail: str,
        active_model_name: str | None = None,
        apply_model_to_team_members: bool = False,
    ) -> DynamicContinuationRunState:
        """Build the continuation state for one turn's first attempt."""
        return cls(
            original_prompt=prompt,
            active_prompt=prompt,
            active_model_prompt=model_prompt,
            active_current_timestamp_ms=current_timestamp_ms,
            active_current_prompt_is_structured=current_prompt_is_structured,
            active_current_event_id=current_event_id,
            active_run_id=run_id,
            continuation_model_prompt_tail=continuation_model_prompt_tail,
            active_model_name=active_model_name,
            apply_model_to_team_members=apply_model_to_team_members,
        )

    def advance(
        self,
        *,
        continuation_prompt: str,
        previous_run_id: str | None,
        active_model_name: str | None,
        apply_model_to_team_members: bool,
    ) -> DynamicContinuationRunState:
        """Return the continuation state for one more same-turn attempt."""
        return replace(
            self,
            active_prompt=continuation_prompt,
            active_model_prompt=self.continuation_model_prompt_tail or None,
            active_current_timestamp_ms=None,
            active_current_prompt_is_structured=False,
            active_current_event_id=None,
            active_run_id=ai_runtime.next_retry_run_id(previous_run_id),
            active_model_name=active_model_name,
            apply_model_to_team_members=apply_model_to_team_members,
        )


@dataclass(frozen=True)
class ResponseTurnContext:
    """Per-turn Matrix identity constants for one response turn.

    Built once by the caller that owns the turn, then passed unchanged through
    the entity envelope, its prepare chain, and the turn drivers. For agent
    turns ``entity_label`` is the configured agent name; team turns refine it
    to the materialized team label via ``dataclasses.replace``.
    """

    entity_label: str
    session_id: str | None
    run_id: str | None
    correlation_id: str
    reply_to_event_id: str | None
    room_id: str | None
    thread_id: str | None
    requester_id: str | None
    matrix_run_metadata: dict[str, Any] | None
    active_model_name: str | None = None
    active_event_ids: frozenset[str] = frozenset()
    transient_enrichment_items: tuple[EnrichmentItem, ...] = ()
    system_enrichment_items: tuple[EnrichmentItem, ...] = ()
    allow_no_report_response: bool = False
    # Set only for scheduled fires that carry a history limit; identifies the
    # prompt-owning event while capping this turn without changing authored config.
    scheduled_history_budget: ScheduledHistoryBudget | None = None


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
    runtime_model_name: str | None = None
    output_tokens: int | None = None
    tool_executions: tuple[ToolExecution, ...] = ()
    completed_tools: tuple[ToolTraceEntry, ...] = ()
    metadata_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExcludedAttempt:
    """One attempt whose provider result is excluded from model history."""

    original_status: RunStatus = RunStatus.cancelled
    response_text: str = ""
    reason: str | None = None
    partial_text: str = ""
    completed_tools: tuple[ToolTraceEntry, ...] = ()
    interrupted_tools: tuple[ToolTraceEntry, ...] = ()
    session_id: str | None = None
    run_id: str | None = None
    metadata_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class PausedAttempt:
    """One Agno run durably paused before approval-gated tool execution."""

    session_id: str
    run_id: str
    tools: tuple[ToolExecution, ...]
    requirements: tuple[RunRequirement, ...] = ()
    runtime_model_name: str | None = None
    team_member_model_names: tuple[tuple[str, str], ...] = ()
    response_text: str = ""
    acknowledged_response_text: str | None = None
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    response_presentation_state: dict[str, object] = field(default_factory=dict)


class ResponsePausedForApproval(StreamingLifecycleSuspensionError):  # noqa: N818
    """Leave the live response turn while its persisted Agno run awaits approval."""

    def __init__(self, paused: PausedAttempt) -> None:
        super().__init__(f"Run {paused.run_id} is waiting for tool approval")
        self.paused = paused

    def capture_collected_presentation(
        self,
        *,
        response_text: str,
        tool_trace: Sequence[ToolTraceEntry],
    ) -> None:
        """Attach an ordered in-memory presentation from a non-Matrix collector."""
        self.capture_presentation(
            StreamingPresentation(
                response_text=response_text,
                tool_trace=tuple(deepcopy(tool_trace)),
            ),
        )


def paused_attempt_from_response(
    response: RunOutput | TeamRunOutput,
    *,
    fallback_session_id: str | None,
    fallback_run_id: str | None,
) -> PausedAttempt | None:
    """Extract confirmation requirements from one persisted paused Agno run."""
    if response.status != RunStatus.paused:
        return None
    return _paused_attempt(
        tools=response.tools or (),
        requirements=response.requirements or (),
        session_id=response.session_id or fallback_session_id,
        run_id=response.run_id or fallback_run_id,
    )


def paused_attempt_from_event(
    event: RunPausedEvent | TeamRunPausedEvent,
    *,
    fallback_session_id: str | None,
    fallback_run_id: str | None,
) -> PausedAttempt | None:
    """Extract confirmation requirements from one streamed Agno pause event."""
    return _paused_attempt(
        tools=event.tools or (),
        requirements=event.requirements or (),
        session_id=event.session_id or fallback_session_id,
        run_id=event.run_id or fallback_run_id,
    )


def _paused_attempt(
    *,
    tools: Sequence[ToolExecution],
    requirements: Sequence[RunRequirement],
    session_id: str | None,
    run_id: str | None,
) -> PausedAttempt | None:
    """Build one restartable pause from Agno's common pause fields."""
    if any(_has_unsupported_approval_requirement(requirement) for requirement in requirements):
        msg = "Paused run contains an unsupported non-confirmation requirement"
        raise RuntimeError(msg)
    pending_requirement_candidates = tuple(
        requirement for requirement in requirements if requirement.needs_confirmation
    )
    if any(
        requirement.tool_execution is None or not requirement.tool_execution.tool_call_id
        for requirement in pending_requirement_candidates
    ):
        msg = "Paused approval requirement is missing its exact tool-call ID"
        raise RuntimeError(msg)
    requirement_call_ids = [
        requirement.tool_execution.tool_call_id
        for requirement in pending_requirement_candidates
        if requirement.tool_execution is not None
    ]
    if len(requirement_call_ids) != len(set(requirement_call_ids)):
        msg = "Paused approval requirements contain duplicate tool-call IDs"
        raise RuntimeError(msg)
    pending_tool_candidates = tuple(tool for tool in tools if tool.requires_confirmation and tool.confirmed is None)
    if any(not tool.tool_call_id for tool in pending_tool_candidates):
        msg = "Paused approval tool is missing its exact tool-call ID"
        raise RuntimeError(msg)
    tool_call_ids = [tool.tool_call_id for tool in pending_tool_candidates]
    if len(tool_call_ids) != len(set(tool_call_ids)):
        msg = "Paused approval tools contain duplicate tool-call IDs"
        raise RuntimeError(msg)
    pending_requirements = pending_requirement_candidates
    pending_tools = list(pending_tool_candidates)
    known_call_ids = {tool.tool_call_id for tool in pending_tools}
    for requirement in pending_requirements:
        tool = requirement.tool_execution
        if tool is not None and tool.tool_call_id not in known_call_ids:
            pending_tools.append(tool)
            known_call_ids.add(tool.tool_call_id)
    if not pending_tools or session_id is None or run_id is None:
        return None
    return PausedAttempt(
        session_id=session_id,
        run_id=run_id,
        tools=tuple(pending_tools),
        requirements=pending_requirements,
    )


@dataclass(frozen=True)
class HandledAttempt:
    """One streaming error whose user-facing text was already emitted."""


BlockingAttemptResolution = CompletedAttempt | ExcludedAttempt | PausedAttempt
StreamAttemptResolution = CompletedAttempt | ExcludedAttempt | PausedAttempt | HandledAttempt


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
    original_status: RunStatus


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
    finalize_attempt: Callable[[ScopeSessionContext | None], Awaitable[None]] | None = None
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
    finalize_attempt: Callable[[ScopeSessionContext | None], Awaitable[None]] | None = None
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


def _fallback_matrix_run_metadata(
    ctx: ResponseTurnContext,
    run: TurnRunState,
    *,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build run metadata for interruptions that fire before prepare finished."""
    return build_matrix_run_metadata(
        ctx.reply_to_event_id,
        run.unseen_event_ids,
        room_id=ctx.room_id,
        thread_id=ctx.thread_id,
        requester_id=ctx.requester_id,
        correlation_id=ctx.correlation_id,
        extra_metadata=deepcopy(ctx.matrix_run_metadata if extra_metadata is None else extra_metadata),
    )


def _interrupted_run_metadata(
    ctx: ResponseTurnContext,
    sinks: TurnSinks,
    run: TurnRunState,
) -> dict[str, Any] | None:
    """Keep the best Matrix metadata available when an attempt is interrupted."""
    if run.run_metadata is not None:
        return run.run_metadata
    if sinks.turn_recorder is not None and sinks.turn_recorder.run_metadata is not None:
        return _fallback_matrix_run_metadata(
            ctx,
            run,
            extra_metadata=sinks.turn_recorder.run_metadata,
        )
    return _fallback_matrix_run_metadata(ctx, run)


def _persist_excluded_attempt_replay(
    ctx: ResponseTurnContext,
    persist: Callable[[ScopeSessionContext | None, StandaloneReplaySnapshot], None],
    run: TurnRunState,
    resolution: ExcludedAttempt,
    run_metadata: dict[str, Any] | None,
) -> None:
    persist(
        run.scope_context,
        StandaloneReplaySnapshot(
            session_id=resolution.session_id or ctx.session_id,
            run_id=resolution.run_id or str(uuid4()),
            partial_text=resolution.partial_text,
            completed_tools=run.turn_state.completed_tools_for(resolution.completed_tools),
            interrupted_tools=list(resolution.interrupted_tools),
            run_metadata=run_metadata,
            original_status=resolution.original_status,
        ),
    )
    run.standalone_replay_persisted = True


def _record_turn_excluded_fallback(
    ctx: ResponseTurnContext,
    persist: Callable[[ScopeSessionContext | None, StandaloneReplaySnapshot], None] | None,
    sinks: TurnSinks,
    run: TurnRunState,
    snapshot: TurnPartialSnapshot,
    *,
    original_status: RunStatus,
    use_recorder_state: bool,
) -> None:
    recorder = sinks.turn_recorder
    if recorder is not None:
        if recorder.outcome == "completed":
            return
        run_metadata = _interrupted_run_metadata(ctx, sinks, run)
        if use_recorder_state:
            run.turn_state.record_interrupted_from_recorder(
                recorder,
                run_metadata=run_metadata,
                original_status=original_status,
            )
        else:
            run.turn_state.record_interrupted(
                recorder,
                run_metadata=run_metadata,
                assistant_text=snapshot.assistant_text,
                completed_tools=list(snapshot.completed_tools),
                interrupted_tools=list(snapshot.interrupted_tools),
                original_status=original_status,
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
            run_metadata=_interrupted_run_metadata(ctx, sinks, run),
            original_status=original_status,
        ),
    )
    run.standalone_replay_persisted = True


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
    if run.empty_response_retried or continuation_count >= DYNAMIC_TOOL_CONTINUATION_LIMIT:
        return False
    discard_empty_run(
        run.scope_context,
        EmptyRunDiscard(
            session_id=resolution.session_id or ctx.session_id,
            run_id=resolution.run_id,
            output_tokens=resolution.output_tokens,
        ),
    )
    run.empty_response_retried = True
    release_attempt_entity(run.scope_context)
    return True


def _advance_turn_continuation(
    sinks: TurnSinks,
    release_attempt_entity: Callable[[ScopeSessionContext | None], None],
    run: TurnRunState,
    resolution: CompletedAttempt,
    continuation: DynamicContinuationRunState,
    *,
    next_prompt: str | None,
    active_model_name: str | None,
    apply_model_to_team_members: bool,
) -> DynamicContinuationRunState:
    """Close the spent attempt entity and prepare run state for one more continuation."""
    completed_tools_for_turn = run.turn_state.completed_tools_for(resolution.completed_tools)
    release_attempt_entity(run.scope_context)
    advanced = continuation.advance(
        continuation_prompt=next_prompt or continuation.original_prompt,
        previous_run_id=resolution.attempt_run_id,
        active_model_name=active_model_name,
        apply_model_to_team_members=apply_model_to_team_members,
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
                        await adapter.finalize_attempt(run.scope_context)
                if isinstance(settled, str):
                    return settled
                continuation = settled
            _raise_continuation_budget_exhausted()
    except asyncio.CancelledError:
        # The blocking envelope re-records from the recorder's canonical state so
        # an in-attempt cancellation (recorded above with attempt-local partials)
        # and an external task cancel converge on the same interrupted turn.
        _record_turn_excluded_fallback(
            ctx,
            adapter.persist_standalone_replay,
            sinks,
            run,
            adapter.snapshot_partial(),
            original_status=RunStatus.cancelled,
            use_recorder_state=True,
        )
        raise
    except ResponsePausedForApproval:
        raise
    except Exception as e:
        _record_turn_excluded_fallback(
            ctx,
            adapter.persist_standalone_replay,
            sinks,
            run,
            adapter.snapshot_partial(),
            original_status=RunStatus.error,
            use_recorder_state=False,
        )
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
    # The blocking envelope publishes run metadata before recording, and only
    # for attempts that end the turn: a discarded empty run's or a superseded
    # continuation attempt's payload must not ride out on a later resolution.
    if isinstance(resolution, PausedAttempt):
        if sinks.turn_recorder is not None:
            sinks.turn_recorder.mark_suspended()
        raise ResponsePausedForApproval(resolution)
    if isinstance(resolution, ExcludedAttempt):
        _publish_run_metadata(sinks, resolution.metadata_content)
        run_metadata = _interrupted_run_metadata(ctx, sinks, run)
        run.turn_state.record_interrupted(
            sinks.turn_recorder,
            run_metadata=run_metadata,
            assistant_text=resolution.partial_text,
            completed_tools=list(resolution.completed_tools),
            interrupted_tools=list(resolution.interrupted_tools),
            original_status=resolution.original_status,
        )
        if sinks.turn_recorder is None and adapter.persist_standalone_replay is not None:
            _persist_excluded_attempt_replay(
                ctx,
                adapter.persist_standalone_replay,
                run,
                resolution,
                run_metadata,
            )
        if resolution.original_status is RunStatus.cancelled:
            raise build_cancelled_error(resolution.reason)
        return resolution.response_text
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
    _publish_run_metadata(sinks, resolution.metadata_content)
    run.turn_state.record_completed(
        sinks.turn_recorder,
        run_metadata=run.run_metadata,
        assistant_text=settle.recorded_text,
        completed_tools=list(settle.recorded_tools),
    )
    return settle.response_text


def _publish_run_metadata(sinks: TurnSinks, metadata_content: dict[str, Any] | None) -> None:
    """Publish one attempt's run metadata payload to the caller's collector."""
    if sinks.run_metadata_collector is not None and metadata_content is not None:
        sinks.run_metadata_collector.update(metadata_content)


@dataclass(frozen=True)
class _CompletionSettle:
    """Driver-internal plan for finishing one completed attempt."""

    keep_going: bool
    continuation: DynamicContinuationRunState
    # What the recorder persists as the turn's replayable assistant text; it
    # can differ from response_text (e.g. tool-rendered visible output).
    recorded_text: str
    recorded_tools: tuple[ToolTraceEntry, ...]
    # The turn's deliverable final text (notice, limit message, or terminal
    # document): the blocking driver returns it; the streaming driver emits it
    # as a final chunk when non-empty (live-streamed attempts leave it empty
    # because their chunks already delivered the document).
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
    if resolution.is_empty and ctx.allow_no_report_response:
        return _CompletionSettle(
            keep_going=False,
            continuation=continuation,
            recorded_text="",
            recorded_tools=(),
            response_text="",
        )
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
                recorded_text="",
                recorded_tools=(),
                response_text="",
            )
        # The notice falls through: run metadata and recorder completion
        # still apply to this notice-only turn.
        return _CompletionSettle(
            keep_going=False,
            continuation=continuation,
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
                active_model_name=(
                    decision.model_switch_name
                    if decision.model_switch_when == "after-toolcall"
                    else resolution.runtime_model_name
                    if decision.model_switch_when == "next-turn"
                    else continuation.active_model_name
                ),
                apply_model_to_team_members=decision.model_switch_when == "after-toolcall",
            ),
            recorded_text="",
            recorded_tools=(),
            response_text="",
        )
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
            recorded_text = decision.limit_message
            response_text = decision.limit_message
    elif ctx.allow_no_report_response and not resolution.replayable_text.strip():
        # Tool presentation and team fallback chrome are not semantic prose.
        # The tool records remain part of the completed turn, but quiet
        # delivery has no final assistant body to publish.
        response_text = ""
    return _CompletionSettle(
        keep_going=False,
        continuation=continuation,
        recorded_text=recorded_text,
        recorded_tools=resolution.completed_tools,
        response_text=response_text,
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
                    if isinstance(resolution, PausedAttempt):
                        if sinks.turn_recorder is not None:
                            sinks.turn_recorder.mark_suspended()
                        raise ResponsePausedForApproval(resolution)
                    if isinstance(resolution, HandledAttempt):
                        _record_turn_excluded_fallback(
                            ctx,
                            adapter.persist_standalone_replay,
                            sinks,
                            run,
                            adapter.snapshot_partial(),
                            original_status=RunStatus.error,
                            use_recorder_state=False,
                        )
                        return
                    if isinstance(resolution, ExcludedAttempt):
                        run_metadata = _interrupted_run_metadata(ctx, sinks, run)
                        run.turn_state.record_interrupted(
                            sinks.turn_recorder,
                            run_metadata=run_metadata,
                            assistant_text=resolution.partial_text,
                            completed_tools=list(resolution.completed_tools),
                            interrupted_tools=list(resolution.interrupted_tools),
                            original_status=resolution.original_status,
                        )
                        _publish_run_metadata(sinks, resolution.metadata_content)
                        if sinks.turn_recorder is None and adapter.persist_standalone_replay is not None:
                            _persist_excluded_attempt_replay(
                                ctx,
                                adapter.persist_standalone_replay,
                                run,
                                resolution,
                                run_metadata,
                            )
                        if resolution.original_status is RunStatus.cancelled:
                            raise build_cancelled_error(resolution.reason)
                        if resolution.response_text:
                            yield adapter.make_text_chunk(resolution.response_text)
                        return
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
                    if settle.response_text:
                        yield adapter.make_text_chunk(settle.response_text)
                    if not keep_going:
                        _publish_run_metadata(sinks, resolution.metadata_content)
                        run.turn_state.record_completed(
                            sinks.turn_recorder,
                            run_metadata=run.run_metadata,
                            assistant_text=settle.recorded_text,
                            completed_tools=list(settle.recorded_tools),
                        )
                finally:
                    if adapter.finalize_attempt is not None:
                        await adapter.finalize_attempt(run.scope_context)
                if not keep_going:
                    return
            _raise_continuation_budget_exhausted()
    except asyncio.CancelledError:
        _record_turn_excluded_fallback(
            ctx,
            adapter.persist_standalone_replay,
            sinks,
            run,
            adapter.snapshot_partial(),
            original_status=RunStatus.cancelled,
            use_recorder_state=False,
        )
        raise
    except ResponsePausedForApproval:
        raise
    except Exception as e:
        _record_turn_excluded_fallback(
            ctx,
            adapter.persist_standalone_replay,
            sinks,
            run,
            adapter.snapshot_partial(),
            original_status=RunStatus.error,
            use_recorder_state=False,
        )
        if adapter.unexpected_error_text is None:
            raise
        logger.exception("Response turn failed", entity=ctx.entity_label)
        yield adapter.make_text_chunk(adapter.unexpected_error_text(e))
        return
    finally:
        adapter.close_runtime_dbs(run.scope_context)
