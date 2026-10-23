"""Shared response-turn drivers for the agent and team envelopes.

One inbound response turn has the same lifecycle regardless of which entity
answers it: open the scope session, run prepared attempts, decide whether the
turn continues after a model switch or a discarded empty run, record the
outcome on the turn recorder, and persist an interrupted replay when the turn
is cancelled without a recorder.
This module owns that lifecycle exactly once; ``mindroom.ai`` and
``mindroom.teams`` supply thin adapters carrying the entity-specific attempt
bodies as injected callables.
It also builds the run metadata and the request-log context a turn binds around
its provider requests and stream pulls.

``ResponseTurnContext`` is the per-turn identity carrier built once by the
caller that owns the turn: the drivers consume its Matrix-identity fields,
while ``active_event_ids`` and enrichment items ride along for the entity
prepare chains. Mutable per-turn state (collectors, recorders, callbacks) stays
out of it and crosses via ``TurnSinks`` or the adapters.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, NoReturn
from uuid import uuid4

from mindroom import ai_runtime
from mindroom.agent_cli.lifetime import CliTurnLifetime, response_cli_lifetime
from mindroom.ai_turn_state import AITurnState
from mindroom.background_tasks import run_blocking_until_complete, wait_for_future_until_complete
from mindroom.cancellation import build_cancelled_error
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
    MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY,
)
from mindroom.delegation.state import DelegationState
from mindroom.helper_usage import helper_usage_context
from mindroom.history.storage import remove_history_of_redacted_events
from mindroom.llm.request_context import bind_llm_request_log_context
from mindroom.llm.runs import RunRequirement, RunStatus, ToolExecution
from mindroom.logging_config import get_logger
from mindroom.streaming import StreamingLifecycleSuspensionError, StreamingPresentation
from mindroom.tool_jobs.consumption import finalize_consumption, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_system.context_bound_streams import closing_async_stream, context_bound_async_stream
from mindroom.turn_continuation import TURN_CONTINUATION_LIMIT, continuation_decision_from_tools

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from mindroom.agent_modes import AgentMode
    from mindroom.delegation.state import SubagentPersona
    from mindroom.dispatch_source import ScheduledHistoryBudget
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.hooks import EnrichmentItem
    from mindroom.llm.request_context import RequestAttribution
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.participation import ParticipationGate
    from mindroom.skill_learning.capture import SkillReviewCapture
    from mindroom.tool_system.events import ToolTraceEntry

logger = get_logger(__name__)

__all__ = [
    "AttemptResolved",
    "BlockingAttemptResolution",
    "BlockingTurnAdapter",
    "CompletedApprovalRun",
    "CompletedAttempt",
    "EmptyRunDiscard",
    "ExcludedAttempt",
    "HandledAttempt",
    "PausedAnswer",
    "PausedAttempt",
    "ResponsePausedForApproval",
    "ResponseTurnContext",
    "ResumedAttempt",
    "SkippedAttempt",
    "StandaloneReplaySnapshot",
    "StreamAttemptResolution",
    "StreamingTurnAdapter",
    "TurnContinuationRunState",
    "TurnPartialSnapshot",
    "TurnRunState",
    "TurnSinks",
    "apply_exact_approval_decisions",
    "build_llm_request_log_context",
    "build_matrix_run_metadata",
    "local_approval_requirements",
    "pause_without_approval_error",
    "paused_attempt_from_response",
    "run_blocking_response_turn",
    "skip_unapproved_attempt",
    "stream_response_turn",
    "stream_with_llm_request_log_context",
]


@dataclass(frozen=True, slots=True)
class CompletedApprovalRun:
    """One completed persisted run plus its normal Matrix response metadata."""

    response_text: str
    metadata_content: dict[str, Any]


def apply_exact_approval_decisions(
    requirements: Sequence[RunRequirement],
    *,
    decisions: Mapping[str, bool],
    denial_reasons: Mapping[str, str | None],
) -> list[RunRequirement]:
    """Apply decisions only when they exactly identify one persisted call each."""
    pending = [requirement for requirement in requirements if requirement.needs_confirmation]
    call_ids: list[str] = []
    for requirement in pending:
        tool = requirement.tool_execution
        if not tool.tool_call_id:
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
            requirement.reject(denial_reasons[tool_call_id])
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


def build_llm_request_log_context(
    attribution: RequestAttribution,
    *,
    prompt: str,
    model_prompt: str | None,
    full_prompt: str,
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    """Return the request-log context for one run's provider requests: its attribution, prompts, and source events."""
    context: dict[str, object] = {name: value for name, value in asdict(attribution).items() if value is not None}
    context["current_turn_prompt"] = prompt
    if model_prompt is not None:
        context["model_prompt"] = model_prompt
    context["full_prompt"] = full_prompt
    if not metadata:
        return context

    source_event_ids = _normalized_string_list(
        [
            attribution.reply_to_event_id,
            *_normalized_string_list(metadata.get(MATRIX_SOURCE_EVENT_IDS_METADATA_KEY)),
        ],
    )
    if source_event_ids:
        context["source_event_ids"] = source_event_ids

    raw_prompt_map = metadata.get(MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY)
    if isinstance(raw_prompt_map, dict):
        source_event_prompts = {
            event_id: event_prompt
            for event_id, event_prompt in raw_prompt_map.items()
            if isinstance(event_id, str) and event_id and isinstance(event_prompt, str)
        }
        if source_event_prompts:
            context["source_event_prompts"] = source_event_prompts

    return context


def stream_with_llm_request_log_context[StreamEventT](
    stream_generator: AsyncIterator[StreamEventT],
    *,
    request_context: dict[str, object],
) -> AsyncIterator[StreamEventT]:
    """Advance one async stream with request-log context bound per item pull."""
    return context_bound_async_stream(
        context_factory=lambda: bind_llm_request_log_context(**request_context),
        stream_factory=stream_generator.__aiter__,
    )


@dataclass(frozen=True)
class TurnContinuationRunState:
    """Prompt and run identity for a same-turn continuation sequence."""

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
    ) -> TurnContinuationRunState:
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
    ) -> TurnContinuationRunState:
        """Return the continuation state for one more same-turn attempt."""
        return replace(
            self,
            active_prompt=continuation_prompt,
            active_model_prompt=self.continuation_model_prompt_tail or None,
            active_current_timestamp_ms=None,
            active_current_prompt_is_structured=False,
            active_current_event_id=None,
            active_run_id=None if previous_run_id is None else str(uuid4()),
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
    # The current message's author when it is not the requester: an entity's reply written for that human.
    current_sender_id: str | None = None
    # Interactive selections reply to the question but consume history through the selecting source.
    history_boundary_event_id: str | None = None
    member_display_names: Mapping[str, str] = field(default_factory=dict)
    active_model_name: str | None = None
    active_event_ids: frozenset[str] = frozenset()
    transient_enrichment_items: tuple[EnrichmentItem, ...] = ()
    system_enrichment_items: tuple[EnrichmentItem, ...] = ()
    allow_no_report_response: bool = False
    background_tool_jobs: bool = False
    # Set for a voice call's turn: no reply owns its calls' outcomes afterwards, so they finish inside it.
    inline_tools: bool = False
    participation: ParticipationGate | None = None
    # Set only for scheduled fires that carry a history limit; identifies the
    # prompt-owning event while capping this turn without changing authored config.
    scheduled_history_budget: ScheduledHistoryBudget | None = None
    agent_mode: AgentMode = "standard"
    # Set only for authored subagents: their authored prompt and tool subset.
    persona: SubagentPersona | None = None
    # Set only for responses that count toward skill learning, so the review can fork their final request.
    skill_review_capture: SkillReviewCapture | None = None
    # Which events this turn's history derives from are redacted, each with the source a legacy summary consumed
    # it through; set for Matrix replies so their history drops them first.
    redacted_history_events: Callable[[tuple[str, ...]], Awaitable[Mapping[str, str | None]]] | None = None


@dataclass(frozen=True)
class TurnSinks:
    """Mutable turn outputs owned by the caller and updated by the drivers."""

    turn_recorder: TurnRecorder | None = None
    run_metadata_collector: dict[str, Any] | None = None
    on_completed: Callable[[CompletedAttempt], None] | None = None


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
    control_executions: tuple[ToolExecution, ...] = ()
    completed_tools: tuple[ToolTraceEntry, ...] = ()
    metadata_content: dict[str, Any] | None = None
    status: RunStatus = RunStatus.completed


@dataclass(frozen=True)
class ResumedAttempt:
    """A restored completed attempt and the logical turn budget already spent."""

    attempt: CompletedAttempt
    continuation_count: int = 0


@dataclass(frozen=True)
class ExcludedAttempt:
    """One attempt whose provider result is excluded from model history."""

    original_status: RunStatus = RunStatus.cancelled
    response_text: str = ""
    reason: str | None = None
    partial_text: str = ""
    completed_tools: tuple[ToolTraceEntry, ...] = ()
    interrupted_tools: tuple[ToolTraceEntry, ...] = ()
    run_id: str | None = None
    metadata_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class PausedAnswer:
    """The answer a paused run showed, as its reply's records keep it, which its resume continues."""

    text: str = ""
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    # A team's structured document, restored instead of re-parsing its rendered text.
    team_state: Mapping[str, object] | None = None


@dataclass(frozen=True)
class PausedAttempt:
    """One run durably paused before approval-gated tool execution."""

    session_id: str
    run_id: str
    tools: tuple[ToolExecution, ...]
    toolkit_owners: Mapping[tuple[str, str], str]
    requirements: tuple[RunRequirement, ...] = ()
    runtime_model_name: str | None = None
    team_member_model_names: tuple[tuple[str, str], ...] = ()
    response_text: str = ""
    acknowledged_response_text: str | None = None
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    response_presentation_state: dict[str, object] = field(default_factory=dict)
    approval_agent_name: str | None = None
    delegation_storage_bindings: dict[str, dict[str, object]] = field(default_factory=dict)
    cli_call: dict[str, object] | None = None
    continuation_count: int = 0


class ResponsePausedForApproval(StreamingLifecycleSuspensionError):  # noqa: N818
    """Leave the live response turn while its persisted run awaits approval."""

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


def pause_without_approval_error(record: AnyRunRecord, *, entity_label: str) -> RuntimeError:
    """Return the error for a pause with no approval to ask for and no delegation to drive.

    Only the delegate tools run outside the loop, and the delegation driver resumes a run paused on them; a run
    paused on any other externally executed call has nothing that can resume it, so the reply fails loudly. The
    loop stored the ``PAUSED`` row; the reply's lifecycle owner then records the failure over it, and a blocking
    reply also raises it through the runner.
    """
    pending = [
        requirement.tool_execution.tool_name
        for requirement in record.requirements or ()
        if not requirement.is_resolved()
    ]
    msg = f"{entity_label} run {record.run_id!r} paused on {pending} without an approval to ask for"
    return RuntimeError(msg)


def local_approval_requirements(record: AnyRunRecord) -> list[RunRequirement]:
    """Return copies of the requirements this actor's approval cards ask about.

    A delegation driver that asked about its own calls lists them as pending requirements; projected child calls
    belong to the child executor, so a run waiting on a child has none of its own.
    """
    delegation = DelegationState.from_metadata(record.metadata)
    if delegation.pending_child_id is not None:
        return []
    if delegation.pending_requirements:
        return [RunRequirement.from_dict(item) for item in delegation.pending_requirements]
    return deepcopy(record.requirements or [])


def paused_attempt_from_response(
    record: AnyRunRecord,
    *,
    fallback_session_id: str | None,
    fallback_run_id: str | None,
    toolkit_owners: Mapping[tuple[str, str], str],
) -> PausedAttempt | None:
    """Extract confirmation requirements from one paused run record."""
    if record.status is not RunStatus.paused:
        return None
    delegation = DelegationState.from_metadata(record.metadata)
    if delegation.pending_tools:
        if delegation.pending_child_id is not None:
            # A source that recorded no toolkit leaves its call unowned, as a missing key does.
            toolkit_owners = {
                (source.child.child_agent_name, str(tool["tool_name"])): source.toolkit_name
                for tool in delegation.pending_tools
                if (source := delegation.pending_tool_sources.get(str(tool["tool_call_id"]))) is not None
                and source.toolkit_name is not None
            }
        paused = _paused_attempt_from_pause(
            toolkit_owners=toolkit_owners,
            tools=[ToolExecution.from_dict(tool) for tool in delegation.pending_tools],
            requirements=[RunRequirement.from_dict(requirement) for requirement in delegation.pending_requirements],
            session_id=record.session_id or fallback_session_id,
            run_id=record.run_id or fallback_run_id,
        )
        return (
            replace(
                paused,
                approval_agent_name=delegation.pending_agent_name,
                delegation_storage_bindings=delegation.storage_bindings,
            )
            if paused is not None
            else None
        )
    return _paused_attempt_from_pause(
        toolkit_owners=toolkit_owners,
        tools=record.tools or (),
        requirements=record.requirements or (),
        session_id=record.session_id or fallback_session_id,
        run_id=record.run_id or fallback_run_id,
    )


def _paused_attempt_from_pause(
    *,
    tools: Sequence[ToolExecution],
    requirements: Sequence[RunRequirement],
    toolkit_owners: Mapping[tuple[str, str], str],
    session_id: str | None,
    run_id: str | None,
) -> PausedAttempt | None:
    """Build one restartable pause from a run's paused tools and confirmation requirements."""
    pending_requirements = tuple(requirement for requirement in requirements if requirement.needs_confirmation)
    if any(not requirement.tool_execution.tool_call_id for requirement in pending_requirements):
        msg = "Paused approval requirement is missing its exact tool-call ID"
        raise RuntimeError(msg)
    requirement_call_ids = [requirement.tool_execution.tool_call_id for requirement in pending_requirements]
    if len(requirement_call_ids) != len(set(requirement_call_ids)):
        msg = "Paused approval requirements contain duplicate tool-call IDs"
        raise RuntimeError(msg)
    pending_tools = [tool for tool in tools if tool.requires_confirmation and tool.confirmed is None]
    if any(not tool.tool_call_id for tool in pending_tools):
        msg = "Paused approval tool is missing its exact tool-call ID"
        raise RuntimeError(msg)
    tool_call_ids = [tool.tool_call_id for tool in pending_tools]
    if len(tool_call_ids) != len(set(tool_call_ids)):
        msg = "Paused approval tools contain duplicate tool-call IDs"
        raise RuntimeError(msg)
    known_call_ids = set(tool_call_ids)
    for requirement in pending_requirements:
        tool = requirement.tool_execution
        if tool.tool_call_id not in known_call_ids:
            pending_tools.append(tool)
            known_call_ids.add(tool.tool_call_id)
    if not pending_tools or session_id is None or run_id is None:
        return None
    return PausedAttempt(
        session_id=session_id,
        run_id=run_id,
        tools=tuple(pending_tools),
        requirements=pending_requirements,
        approval_agent_name=next(
            (
                requirement.member_agent_name
                for requirement in pending_requirements
                if requirement.member_agent_name and requirement.member_agent_id is None
            ),
            None,
        ),
        toolkit_owners=toolkit_owners,
    )


@dataclass(frozen=True)
class HandledAttempt:
    """One streaming error whose user-facing text was already emitted."""

    metadata_content: dict[str, Any] | None = None


@dataclass(frozen=True)
class SkippedAttempt:
    """One unapproved response attempt with no answer or successful-run effects."""

    reason: str
    session_id: str | None = None
    run_id: str | None = None
    output_tokens: int | None = None


BlockingAttemptResolution = CompletedAttempt | ExcludedAttempt | PausedAttempt | SkippedAttempt
StreamAttemptResolution = CompletedAttempt | ExcludedAttempt | PausedAttempt | HandledAttempt | SkippedAttempt


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
        [TurnRunState, TurnContinuationRunState],
        Awaitable[BlockingAttemptResolution],
    ]
    snapshot_partial: Callable[[], TurnPartialSnapshot]
    release_attempt_entity: Callable[[ScopeSessionContext | None], None]
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
        [TurnRunState, TurnContinuationRunState],
        AsyncIterator[ChunkT | AttemptResolved],
    ]
    snapshot_partial: Callable[[], TurnPartialSnapshot]
    release_attempt_entity: Callable[[ScopeSessionContext | None], None]
    discard_empty_run: Callable[[ScopeSessionContext | None, EmptyRunDiscard], None]
    make_text_chunk: Callable[[str], ChunkT]
    on_scope_opened: Callable[[ScopeSessionContext | None], None] | None = None
    finalize_attempt: Callable[[ScopeSessionContext | None], Awaitable[None]] | None = None
    unexpected_error_text: Callable[[Exception], str] | None = None
    persist_standalone_replay: Callable[[ScopeSessionContext | None, StandaloneReplaySnapshot], None] | None = None


def _raise_continuation_budget_exhausted() -> NoReturn:
    # The continuation loop always settles on its final iteration: at the limit
    # the decision carries a limit_message and never asks to continue.
    msg = "turn continuation loop must return within its iteration budget"
    raise AssertionError(msg)


def _raise_missing_stream_resolution(entity_label: str) -> NoReturn:
    # A streaming attempt that returns without its sentinel would otherwise end
    # the turn silently: nothing recorded, no metadata published, no log.
    msg = f"streaming attempt for {entity_label!r} ended without yielding its AttemptResolved sentinel"
    raise RuntimeError(msg)


def _reset_turn_state_for_continuation(
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


def _settle_excluded_attempt(
    ctx: ResponseTurnContext,
    adapter: BlockingTurnAdapter | StreamingTurnAdapter[Any],
    sinks: TurnSinks,
    run: TurnRunState,
    resolution: ExcludedAttempt,
) -> str:
    """Record an excluded attempt as the turn's interrupted outcome and return its reply text; a cancellation raises."""
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
        adapter.persist_standalone_replay(
            run.scope_context,
            StandaloneReplaySnapshot(
                run_id=resolution.run_id or str(uuid4()),
                partial_text=resolution.partial_text,
                completed_tools=run.turn_state.completed_tools_for(resolution.completed_tools),
                interrupted_tools=list(resolution.interrupted_tools),
                run_metadata=run_metadata,
                original_status=resolution.original_status,
            ),
        )
        run.standalone_replay_persisted = True
    if resolution.original_status is RunStatus.cancelled:
        raise build_cancelled_error(resolution.reason)
    return resolution.response_text


def _failed_turn_text(
    ctx: ResponseTurnContext,
    adapter: BlockingTurnAdapter | StreamingTurnAdapter[Any],
    sinks: TurnSinks,
    run: TurnRunState,
    error: Exception,
) -> str | None:
    """Settle a turn that raised: return its error reply, None when it is skipped before participation.

    Raises ``error`` again, after recording the interrupted turn, when the adapter has no error reply.
    """
    if skipped := skip_unapproved_attempt(ctx.participation, reason="run_failed_before_decision"):
        _settle_skipped_attempt(ctx, sinks, run, skipped, adapter.discard_empty_run)
        logger.exception("Response turn skipped before participation", entity=ctx.entity_label)
        return None
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
        raise error
    logger.exception("Response turn failed", entity=ctx.entity_label)
    return adapter.unexpected_error_text(error)


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
    """Discard the empty completed run; return whether one retry is granted.

    Every empty run is discarded, including the retry's: a persisted assistant
    turn with no content teaches the model that ending immediately is the
    expected continuation. The one-shot retry borrows a continuation slot so
    the outer loop's iteration budget stays authoritative; a granted retry
    closes the spent entity's runtime state exactly like the continuation
    handoff.
    """
    discard_empty_run(
        run.scope_context,
        EmptyRunDiscard(
            session_id=resolution.session_id or ctx.session_id,
            run_id=resolution.run_id,
            output_tokens=resolution.output_tokens,
        ),
    )
    if run.empty_response_retried or continuation_count >= TURN_CONTINUATION_LIMIT:
        return False
    run.empty_response_retried = True
    release_attempt_entity(run.scope_context)
    return True


def _advance_turn_continuation(
    sinks: TurnSinks,
    release_attempt_entity: Callable[[ScopeSessionContext | None], None],
    run: TurnRunState,
    resolution: CompletedAttempt,
    continuation: TurnContinuationRunState,
    *,
    next_prompt: str | None,
    active_model_name: str | None,
    apply_model_to_team_members: bool,
) -> TurnContinuationRunState:
    """Close the spent attempt entity and prepare run state for one more continuation."""
    completed_tools_for_turn = run.turn_state.completed_tools_for(resolution.completed_tools)
    release_attempt_entity(run.scope_context)
    advanced = continuation.advance(
        continuation_prompt=next_prompt or continuation.original_prompt,
        previous_run_id=resolution.attempt_run_id,
        active_model_name=active_model_name,
        apply_model_to_team_members=apply_model_to_team_members,
    )
    run.turn_state = _reset_turn_state_for_continuation(
        turn_recorder=sinks.turn_recorder,
        run_metadata=run.run_metadata,
        completed_tools_for_turn=completed_tools_for_turn,
    )
    return advanced


async def _remove_history_of_redacted_events(
    ctx: ResponseTurnContext,
    scope_context: ScopeSessionContext | None,
) -> None:
    """Remove persisted history derived from events since redacted, before the history is used.

    This is the only cleanup after a redaction: every event this scope's history derives
    from is checked against the room's and the conversation's tombstones each time a
    response opens it, which also covers a message no turn answered but a later turn read
    as context.
    """
    if ctx.redacted_history_events is None or scope_context is None or scope_context.session is None:
        return
    removed_event_ids = await remove_history_of_redacted_events(
        scope_context.storage,
        scope_context.session,
        scope_context.scope,
        ctx.redacted_history_events,
    )
    if removed_event_ids:
        logger.info(
            "Removed history derived from redacted events",
            session_id=scope_context.session.session_id,
            history_scope=scope_context.scope.key,
            redacted_event_ids=removed_event_ids,
        )


def _enter_scope_context(
    open_scope: Callable[[], AbstractContextManager[ScopeSessionContext | None]],
) -> tuple[AbstractContextManager[ScopeSessionContext | None], ScopeSessionContext | None]:
    """Enter one synchronous scope context on a worker thread."""
    manager = open_scope()
    return manager, manager.__enter__()


@asynccontextmanager
async def _open_scope_off_event_loop(
    open_scope: Callable[[], AbstractContextManager[ScopeSessionContext | None]],
) -> AsyncIterator[ScopeSessionContext | None]:
    """Keep synchronous session storage open and close work off the event loop."""
    entry_task = asyncio.create_task(asyncio.to_thread(_enter_scope_context, open_scope))
    try:
        manager, scope_context = await wait_for_future_until_complete(entry_task)
    except asyncio.CancelledError as cancellation:
        # The guarded wait drains the entry task before propagating cancellation.
        # If entry succeeded, close the newly opened storage before returning it.
        try:
            manager, _scope_context = entry_task.result()
        except BaseException as entry_error:
            raise cancellation from entry_error
        try:
            await run_blocking_until_complete(
                manager.__exit__,
                type(cancellation),
                cancellation,
                cancellation.__traceback__,
            )
        except BaseException as exit_error:
            raise cancellation from exit_error
        raise

    try:
        yield scope_context
    except BaseException:
        if not await run_blocking_until_complete(manager.__exit__, *sys.exc_info()):
            raise
    else:
        await run_blocking_until_complete(manager.__exit__, None, None, None)


@owned_tool_execution(enabled=lambda ctx, *_args, **_kwargs: ctx.background_tool_jobs)
async def run_blocking_response_turn(
    ctx: ResponseTurnContext,
    adapter: BlockingTurnAdapter,
    sinks: TurnSinks,
    *,
    continuation: TurnContinuationRunState,
) -> str:
    """Run one blocking response turn to a final user-visible text."""
    run = TurnRunState()
    try:
        async with (
            response_cli_lifetime() as cli_lifetime,
            _open_scope_off_event_loop(adapter.open_scope) as scope_context,
        ):
            await _remove_history_of_redacted_events(ctx, scope_context)
            run.scope_context = scope_context
            set_consumption_storage(scope_context.storage_factory if scope_context is not None else None)
            if adapter.on_scope_opened is not None:
                adapter.on_scope_opened(scope_context)
            for continuation_count in range(TURN_CONTINUATION_LIMIT + 1):
                cli_lifetime.continuation_count = continuation_count
                try:
                    with helper_usage_context(scope_context):
                        resolution = await adapter.run_attempt(run, continuation)
                    await finalize_consumption()
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
                    await cli_lifetime.retire_attempt()
                    if adapter.finalize_attempt is not None:
                        await adapter.finalize_attempt(run.scope_context)
                    await finalize_consumption()
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
        return _failed_turn_text(ctx, adapter, sinks, run, e) or ""


def skip_unapproved_attempt(
    participation: ParticipationGate | None,
    *,
    reason: str,
    session_id: str | None = None,
    run_id: str | None = None,
    output_tokens: int | None = None,
) -> SkippedAttempt | None:
    """Resolve unapproved completion or failure without taking over run cleanup."""
    if participation is None or not participation.decline(reason):
        return None
    return SkippedAttempt(reason=reason, session_id=session_id, run_id=run_id, output_tokens=output_tokens)


def _settle_skipped_attempt(
    ctx: ResponseTurnContext,
    sinks: TurnSinks,
    run: TurnRunState,
    resolution: SkippedAttempt,
    discard_empty_run: Callable[[ScopeSessionContext | None, EmptyRunDiscard], None],
) -> None:
    """Discard the skipped provider run without recording an assistant turn."""
    if resolution.run_id is not None:
        discard_empty_run(
            run.scope_context,
            EmptyRunDiscard(
                session_id=resolution.session_id or ctx.session_id,
                run_id=resolution.run_id,
                output_tokens=resolution.output_tokens,
            ),
        )
    if sinks.turn_recorder is not None:
        sinks.turn_recorder.mark_skipped()


def _settle_blocking_attempt(
    ctx: ResponseTurnContext,
    adapter: BlockingTurnAdapter,
    sinks: TurnSinks,
    run: TurnRunState,
    resolution: BlockingAttemptResolution,
    continuation: TurnContinuationRunState,
    *,
    continuation_count: int,
) -> str | TurnContinuationRunState:
    """Settle one blocking attempt: return final text or the advanced continuation."""
    # The blocking envelope publishes run metadata before recording, and only
    # for attempts that end the turn: a discarded empty run's or a superseded
    # continuation attempt's payload must not ride out on a later resolution.
    if isinstance(resolution, SkippedAttempt):
        _settle_skipped_attempt(ctx, sinks, run, resolution, adapter.discard_empty_run)
        return ""
    if isinstance(resolution, PausedAttempt):
        if sinks.turn_recorder is not None:
            sinks.turn_recorder.mark_suspended()
        raise ResponsePausedForApproval(replace(resolution, continuation_count=continuation_count))
    if isinstance(resolution, ExcludedAttempt):
        return _settle_excluded_attempt(ctx, adapter, sinks, run, resolution)
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
    _record_completed_turn(sinks, run, resolution, settle)
    return settle.response_text


def _publish_run_metadata(sinks: TurnSinks, metadata_content: dict[str, Any] | None) -> None:
    """Publish one attempt's run metadata payload to the caller's collector."""
    if sinks.run_metadata_collector is not None and metadata_content is not None:
        sinks.run_metadata_collector.update(metadata_content)


def _record_completed_turn(
    sinks: TurnSinks,
    run: TurnRunState,
    resolution: CompletedAttempt,
    settle: _CompletionSettle,
) -> None:
    """Publish the turn-ending attempt's run metadata, record the completed turn, and hand the attempt to the caller."""
    _publish_run_metadata(sinks, resolution.metadata_content)
    run.turn_state.record_completed(
        sinks.turn_recorder,
        run_metadata=run.run_metadata,
        assistant_text=settle.recorded_text,
        completed_tools=list(settle.recorded_tools),
    )
    if sinks.on_completed is not None:
        sinks.on_completed(resolution)


@dataclass(frozen=True)
class _CompletionSettle:
    """Driver-internal plan for finishing one completed attempt."""

    keep_going: bool
    continuation: TurnContinuationRunState
    # What the recorder persists as the turn's replayable assistant text; it
    # can differ from response_text (e.g. tool-rendered visible output).
    recorded_text: str = ""
    recorded_tools: tuple[ToolTraceEntry, ...] = ()
    # The turn's deliverable final text (notice, limit message, or terminal
    # document): the blocking driver returns it; the streaming driver emits it
    # as a final chunk when non-empty (live-streamed attempts leave it empty
    # because their chunks already delivered the document).
    response_text: str = ""


def _settle_completed_attempt(
    ctx: ResponseTurnContext,
    sinks: TurnSinks,
    run: TurnRunState,
    resolution: CompletedAttempt,
    continuation: TurnContinuationRunState,
    *,
    discard_empty_run: Callable[[ScopeSessionContext | None, EmptyRunDiscard], None],
    release_attempt_entity: Callable[[ScopeSessionContext | None], None],
    continuation_count: int,
) -> _CompletionSettle:
    """Settle one completed attempt into a record/deliver plan or a continuation."""
    if resolution.is_empty and ctx.allow_no_report_response:
        return _CompletionSettle(keep_going=False, continuation=continuation)
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
            return _CompletionSettle(keep_going=True, continuation=continuation)
        # The notice falls through: run metadata and recorder completion
        # still apply to this notice-only turn.
        return _CompletionSettle(
            keep_going=False,
            continuation=continuation,
            response_text=ai_runtime.EMPTY_RESPONSE_NOTICE,
        )
    decision = continuation_decision_from_tools(
        (*resolution.tool_executions, *resolution.control_executions),
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
        )
    recorded_text = resolution.replayable_text
    response_text = resolution.response_text
    if decision.limit_message is not None:
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


@owned_tool_execution(enabled=lambda ctx, *_args, **_kwargs: ctx.background_tool_jobs)
async def stream_response_turn[ChunkT](
    ctx: ResponseTurnContext,
    adapter: StreamingTurnAdapter[ChunkT],
    sinks: TurnSinks,
    *,
    continuation: TurnContinuationRunState,
    resumed_attempt: ResumedAttempt | None = None,
    initial_continuation_count: int = 0,
) -> AsyncGenerator[ChunkT, None]:
    """Own the whole response while binding context only during pulls and close."""
    lifetime = CliTurnLifetime()
    stream = context_bound_async_stream(
        context_factory=lifetime.bind,
        stream_factory=lambda: _stream_response_turn(
            ctx,
            adapter,
            sinks,
            continuation=continuation,
            cli_lifetime=lifetime,
            resumed_attempt=resumed_attempt,
            initial_continuation_count=initial_continuation_count,
        ),
    )
    try:
        async with closing_async_stream(stream):
            async for chunk in stream:
                yield chunk
    finally:
        await lifetime.close()


async def _stream_response_turn[ChunkT](  # noqa: C901, PLR0912, PLR0915
    ctx: ResponseTurnContext,
    adapter: StreamingTurnAdapter[ChunkT],
    sinks: TurnSinks,
    *,
    continuation: TurnContinuationRunState,
    cli_lifetime: CliTurnLifetime,
    resumed_attempt: ResumedAttempt | None = None,
    initial_continuation_count: int = 0,
) -> AsyncGenerator[ChunkT, None]:
    """Run one streaming response turn, yielding the attempt chunks as they arrive."""
    run = TurnRunState()
    try:
        async with _open_scope_off_event_loop(adapter.open_scope) as scope_context:
            await _remove_history_of_redacted_events(ctx, scope_context)
            run.scope_context = scope_context
            set_consumption_storage(scope_context.storage_factory if scope_context is not None else None)
            if adapter.on_scope_opened is not None:
                adapter.on_scope_opened(scope_context)
            initial_count = (
                resumed_attempt.continuation_count if resumed_attempt is not None else initial_continuation_count
            )
            for continuation_count in range(initial_count, TURN_CONTINUATION_LIMIT + 1):
                cli_lifetime.continuation_count = continuation_count
                resolution: StreamAttemptResolution | None = None
                keep_going = False
                try:
                    if resumed_attempt is not None:
                        resolution = resumed_attempt.attempt
                        resumed_attempt = None
                    else:
                        attempt_stream = context_bound_async_stream(
                            context_factory=lambda: helper_usage_context(scope_context),
                            stream_factory=partial(adapter.run_attempt, run, continuation),
                        )
                        async with closing_async_stream(attempt_stream):
                            async for item in attempt_stream:
                                if isinstance(item, AttemptResolved):
                                    # Drain the producer to finish persistence and cleanup.
                                    resolution = item.resolution
                                    continue
                                yield item
                    await finalize_consumption()
                    if resolution is None:
                        _raise_missing_stream_resolution(ctx.entity_label)
                    if isinstance(resolution, SkippedAttempt):
                        _settle_skipped_attempt(ctx, sinks, run, resolution, adapter.discard_empty_run)
                        return
                    if isinstance(resolution, PausedAttempt):
                        if sinks.turn_recorder is not None:
                            sinks.turn_recorder.mark_suspended()
                        raise ResponsePausedForApproval(replace(resolution, continuation_count=continuation_count))
                    if isinstance(resolution, HandledAttempt):
                        _publish_run_metadata(sinks, resolution.metadata_content)
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
                        if response_text := _settle_excluded_attempt(ctx, adapter, sinks, run, resolution):
                            yield adapter.make_text_chunk(response_text)
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
                        _record_completed_turn(sinks, run, resolution, settle)
                finally:
                    await cli_lifetime.retire_attempt()
                    if adapter.finalize_attempt is not None:
                        await adapter.finalize_attempt(run.scope_context)
                    await finalize_consumption()
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
        failure_text = _failed_turn_text(ctx, adapter, sinks, run, e)
        if failure_text is not None:
            yield adapter.make_text_chunk(failure_text)
