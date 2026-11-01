"""The response-turn driver behind agent and team turns.

One reply turn has the same lifecycle whichever entity answers it: load the
scope session, run prepared attempts, decide whether the turn continues after a
model switch or an empty reply, and settle the turn's record, storing an
interrupted turn once in the scope its runs are stored in. Which of its runs are
stored as turns that later prompts replay is the driver's rule too
(``replays_as_turn``), which each attempt's storage applies to the records its
runs hand over.
This module owns that lifecycle once; ``mindroom.ai`` and ``mindroom.teams``
supply adapters carrying the entity's attempt bodies, and blocking callers read
the same stream through ``collect_streamed_response_content``.
It also builds the run metadata and the request-log context a turn binds around
its provider requests and stream pulls.

``ResponseTurnContext`` holds one turn's inputs, built once by the caller that
owns the turn. The collectors and callbacks a turn reports to cross via
``TurnSinks``, and a resumed attempt via ``TurnResume``.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, NoReturn, Protocol
from uuid import uuid4

from mindroom import ai_runtime
from mindroom.agent_cli.lifetime import CliTurnLifetime
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.cancellation import current_task_is_process_shutdown
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
    MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY,
)
from mindroom.delegation.state import DelegationState
from mindroom.helper_usage import helper_usage_context
from mindroom.history.interrupted_replay import NO_TURN_INPUT, TurnInput, persist_interrupted_replay_snapshot
from mindroom.history.storage import remove_history_of_redacted_events
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.llm.events import RunFinished, TextDelta, ToolCompleted, ToolProgress, ToolStarted
from mindroom.llm.request_context import bind_llm_request_log_context
from mindroom.llm.runs import RunRequirement, RunStatus, ToolExecution
from mindroom.logging_config import get_logger
from mindroom.streaming import StreamingLifecycleSuspensionError, StreamingPresentation, StreamInputChunk
from mindroom.tool_jobs.consumption import finalize_consumption, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_system.context_bound_streams import closing_async_stream, context_bound_async_stream
from mindroom.tool_system.events import CollectedStreamPresentation, FailedReply, StructuredStreamChunk
from mindroom.turn_continuation import TURN_CONTINUATION_LIMIT, continuation_decision_from_tools, stopped_calls

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_modes import AgentMode
    from mindroom.delegation.state import SubagentPersona
    from mindroom.dispatch_source import ScheduledHistoryBudget
    from mindroom.event_journal import ApprovalCall
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.types import CompactionLifecycle
    from mindroom.hooks import EnrichmentItem
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.llm.events import ReportedCall
    from mindroom.llm.request_context import RequestAttribution
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import RunMetrics
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.media_inputs import MediaInputs
    from mindroom.participation import ParticipationGate
    from mindroom.skill_learning.capture import SkillReviewCapture
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

__all__ = [
    "AttemptResolved",
    "CompletedApprovalRun",
    "CompletedAttempt",
    "HandledAttempt",
    "PausedAnswer",
    "PausedAttempt",
    "ResponsePausedForApproval",
    "ResponseTurnContext",
    "SkippedAttempt",
    "StreamAttemptResolution",
    "TurnAdapter",
    "TurnContinuationRunState",
    "TurnPartialSnapshot",
    "TurnResume",
    "TurnRunState",
    "TurnSinks",
    "apply_exact_approval_decisions",
    "build_llm_request_log_context",
    "build_matrix_run_metadata",
    "collect_streamed_response_content",
    "is_empty_reply",
    "own_approved_calls",
    "pause_without_approval_error",
    "paused_attempt_from_response",
    "pending_approval_requirements",
    "replays_as_turn",
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
    calls: Sequence[ApprovalCall],
) -> list[RunRequirement]:
    """Apply the decisions ``calls`` carry only when they exactly identify one persisted call each."""
    pending = [requirement for requirement in requirements if requirement.needs_confirmation]
    call_ids = [requirement.tool_execution.tool_call_id for requirement in pending]
    decided = {call.tool_call_id: call for call in calls}
    if len(call_ids) != len(set(call_ids)) or set(call_ids) != set(decided):
        msg = "Paused tools no longer match the approval continuation"
        raise RuntimeError(msg)
    for requirement, tool_call_id in zip(pending, call_ids, strict=True):
        call = decided[tool_call_id]
        if call.approved:
            requirement.confirm()
        else:
            requirement.reject(call.reason)
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
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
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
    return metadata


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
    # The model a switch for the current response moved the turn to, which its team's members run on too.
    active_model_name: str | None

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
            active_model_name=None,
        )

    def advance(self, *, continuation_prompt: str, active_model_name: str) -> TurnContinuationRunState:
        """Return the continuation state for one more same-turn attempt, on the model a switch moved the turn to."""
        return replace(
            self.next_attempt(),
            active_prompt=continuation_prompt,
            active_model_prompt=self.continuation_model_prompt_tail or None,
            active_current_timestamp_ms=None,
            active_current_prompt_is_structured=False,
            active_current_event_id=None,
            active_model_name=active_model_name,
        )

    def next_attempt(self) -> TurnContinuationRunState:
        """Return the state for one more attempt at the active prompt, which runs under a run id of its own.

        A save under a stored run's id replaces that run's record and usage, which only the run itself may do, as when
        it resumes after a pause, so no attempt reuses the run id of the attempt before it.
        """
        return replace(self, active_run_id=str(uuid4()))


@dataclass(frozen=True)
class ResponseTurnContext:
    """Every input of one response turn: its Matrix identity, its prompt and context, and how it runs.

    Built once by the caller that owns the turn; the entity envelope and its
    prepare chain narrow it with ``dataclasses.replace``, and the turn driver
    reads the result. For agent turns ``entity_label`` is the configured agent
    name; team turns arrive named by the entity whose bot runs them, whose
    timezone dates the turn, and refine it to the materialized team label.
    What the turn writes back (its recorder and collectors) crosses in ``TurnSinks`` instead, except
    ``skill_review_capture``, which the run fills with its final request for the skill review.
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
    # Set only for contact turns: the agent's own tools the turn may offer, with no skills or knowledge.
    tool_names: tuple[str, ...] | None = None
    # Set only for responses that count toward skill learning, so the review can fork their final request.
    skill_review_capture: SkillReviewCapture | None = None
    # Which events this turn's history derives from are redacted, each with the source a legacy summary consumed
    # it through; set for Matrix replies so their history drops them first.
    redacted_history_events: Callable[[tuple[str, ...]], Awaitable[Mapping[str, str | None]]] | None = None
    # The current message: what the requester wrote, and the model-facing prompt around it when it differs.
    prompt: str = ""
    model_prompt: str | None = None
    current_timestamp_ms: float | None = None
    # Whether the current prompt already carries trusted message tags.
    current_prompt_is_structured: bool = False
    thread_history: Sequence[ResolvedVisibleMessage] | None = None
    media: MediaInputs | None = None
    show_tool_calls: bool = True
    # Whether the model's reasoning reaches the reply's stream, which shows it live and keeps it on the final event.
    show_reasoning: bool = False
    # Whose turn it is; it scopes agent state, sessions, memory, and tool runs consistently for this turn.
    execution_identity: ToolExecutionIdentity | None = None
    # The ordered foreground compaction notices of a Matrix reply.
    compaction_lifecycle: CompactionLifecycle | None = None
    pipeline_timing: DispatchPipelineTiming | None = None
    # The runtime's shared knowledge refresher, which delegated children reuse.
    refresh_scheduler: KnowledgeRefreshScheduler | None = None
    # How an agent turn's agent is built: its knowledge, prompt sections, tool policy, and approval support.
    knowledge: AgentKnowledge | None = None
    include_interactive_questions: bool = True
    tool_function_filter: Callable[[ToolFunction], bool] | None = None
    delegation_depth: int = 0
    # Send deferred toolkits as ordinary tools; models with native tool search keep native deferral.
    eager_deferred_tools: bool = False
    supports_native_tool_approval: bool = False
    # A caller-owned agent build reused across sequential turns, as a voice call does.
    reusable_build: AgentBuild | None = None

    @property
    def include_openai_compat_guidance(self) -> bool:
        """Whether the turn answers the OpenAI-compatible API, which omits Matrix sender attribution."""
        return self.execution_identity is not None and self.execution_identity.channel == "openai_compat"


@dataclass(frozen=True)
class TurnSinks:
    """What a turn reports back to its caller, and whether and where it stores an interruption."""

    # The turn's record, which the driver writes and the caller reads once the turn ended; a turn whose caller passes
    # none keeps its own.
    turn_recorder: TurnRecorder | None = None
    run_metadata_collector: dict[str, Any] | None = None
    on_completed: Callable[[CompletedAttempt], None] | None = None
    # Receives the failure the turn answered with a reply text, an attempt's or one no attempt handled.
    on_failed: Callable[[HandledAttempt], None] | None = None
    # Receives each attempt's run id and configured model name before the attempt starts.
    on_attempt_start: Callable[[str, str], None] | None = None
    # Whether the driver stores an interrupted turn in its scope's history; a delegated child's interruption belongs
    # to delegation recovery, and a voice call stores what it spoke.
    persist_interrupted: bool = True
    # The event the turn's Matrix reply shows, which an interrupted record names.
    response_event_id: Callable[[], str | None] | None = None


@dataclass(frozen=True)
class TurnResume:
    """Where a resumed turn starts: the restored attempt to settle first, if any, and the same-turn attempts spent.

    ``prior_usage`` is the usage of the turn's attempts before the first one the resume settles or runs, which the
    reply's usage adds (``TurnRunState.prior_usage``).
    """

    attempt: CompletedAttempt | None = None
    continuation_count: int = 0
    prior_usage: tuple[RunMetrics, ...] = ()


@dataclass(frozen=True)
class TurnPartialSnapshot:
    """Live partial-output view used when a turn is cancelled from outside."""

    assistant_text: str = ""
    completed_tools: tuple[ToolTraceEntry, ...] = ()
    interrupted_tools: tuple[ToolTraceEntry, ...] = ()
    attempt_run_id: str | None = None
    # The input around the prompt as the attempt's run stored it, which a mid-turn fold may have rewritten; None until
    # the run stored a record.
    turn_input: TurnInput | None = None


class _LiveAttempt(Protocol):
    """The attempt a turn is running, as an interruption records it."""

    def partial_snapshot(self) -> TurnPartialSnapshot:
        """Return the attempt's partial output, its run id, and the input its run stored, once it stored one."""
        ...


@dataclass
class TurnRunState:
    """Driver-owned mutable state shared with the adapter attempt bodies.

    Attempt bodies must set ``run_metadata`` and ``turn_input`` once prepare
    finishes; until then interruption recording falls back to rebuilt metadata
    for ``run_metadata`` and to no input for ``turn_input``. They set
    ``attempt`` once their attempt starts, which tells the driver
    the attempt ran in the turn's scope; the driver drops it when the attempt is
    spent, so an interruption before the next attempt starts records none of the
    spent attempt's partial output, whose finished calls ``prior_completed_tools``
    already carries.
    """

    # The turn's record; its run's hooks note the calls a mid-turn fold summarized into it.
    record: TurnRecorder
    # None until the driver loads the turn's scope, which it does before the turn's first attempt.
    scope_context: ScopeSessionContext | None = None
    run_metadata: dict[str, Any] | None = None
    # The input around the prompt the prepared attempt sends, whose context events ``run_metadata`` marks seen. An
    # interrupted record keeps it until the attempt's run stores its own (``TurnPartialSnapshot.turn_input``), as the
    # run of an attempt that completes does.
    turn_input: TurnInput = NO_TURN_INPUT
    # The calls the turn's spent attempts finished, and their usage, before a retry or a continuation started another.
    prior_completed_tools: tuple[ToolTraceEntry, ...] = ()
    prior_usage: tuple[RunMetrics, ...] = ()
    empty_response_retried: bool = False
    attempt: _LiveAttempt | None = None

    def partial_snapshot(self) -> TurnPartialSnapshot:
        """Return the running attempt's partial output, empty while no attempt runs."""
        return self.attempt.partial_snapshot() if self.attempt is not None else TurnPartialSnapshot()

    @property
    def scope(self) -> ScopeSessionContext:
        """Return the turn's scope context, which an attempt body reads once the driver loaded it."""
        assert self.scope_context is not None, "the driver loads the turn's scope before its first attempt"
        return self.scope_context


@dataclass(frozen=True)
class CompletedAttempt:
    """One attempt that ran to a terminal provider response."""

    # Final text the attempt's chunks did not stream: an approval resume's
    # restored text, or a team's no-response line when its chunks showed
    # nothing. The driver emits it once the attempt settles (an in-attempt
    # yield would leak text that an empty-run retry or continuation is about
    # to supersede).
    response_text: str = ""
    replayable_text: str = ""
    has_visible_content: bool = False
    # The run is an empty reply (``is_empty_reply``), as its storage read it (``replays_as_turn``).
    is_empty: bool = False
    run_id: str | None = None
    attempt_run_id: str | None = None
    # The usage the attempt's runs reported, which the turn's later attempts publish with their own once it is spent.
    usage: RunMetrics | None = None
    # The run's own calls, read from its record, and the control calls its hidden CLI calls made.
    tool_executions: tuple[ToolExecution, ...] = ()
    control_calls: tuple[ReportedCall, ...] = ()
    completed_tools: tuple[ToolTraceEntry, ...] = ()
    # Builds the run metadata the reply publishes when the attempt ends the turn: its usage adds the turn's spent
    # attempts' (``TurnRunState.prior_usage``). Building prices the usage, so only a reader of it calls this.
    build_run_metadata: Callable[[], dict[str, Any]] | None = None
    status: RunStatus = RunStatus.completed


@dataclass(frozen=True)
class PausedAnswer:
    """The answer a paused run showed, as its reply's records keep it, which its resume continues."""

    text: str = ""
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    # A team's structured document, restored instead of re-parsing its rendered text.
    team_state: Mapping[str, object] | None = None


@dataclass(frozen=True)
class PausedAttempt:
    """One run durably paused before approval-gated tool execution, with the calls its approval asks about."""

    session_id: str
    run_id: str
    requirements: tuple[RunRequirement, ...]
    toolkit_owners: Mapping[tuple[str, str], str]
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

    @property
    def tools(self) -> tuple[ToolExecution, ...]:
        """The paused calls the approval asks about, in the order its cards show them."""
        return tuple(requirement.tool_execution for requirement in self.requirements)


class ResponsePausedForApproval(StreamingLifecycleSuspensionError):  # noqa: N818
    """Leave the live response turn while its persisted run awaits approval."""

    def __init__(self, paused: PausedAttempt) -> None:
        super().__init__(f"Run {paused.run_id} is waiting for tool approval")
        self.paused = paused


def _apply_collected_chunk(presentation: CollectedStreamPresentation, chunk: StreamInputChunk) -> None:
    """Fold one stream chunk into a collected presentation; a team's document arrives whole in each structured chunk."""
    if isinstance(chunk, str):
        presentation.show_text(chunk)
    elif isinstance(chunk, TextDelta):
        presentation.append_text(chunk.text)
    elif isinstance(chunk, FailedReply):
        presentation.show_text(chunk.text)
    elif isinstance(chunk, StructuredStreamChunk):
        presentation.show_document(chunk)
    elif isinstance(chunk, RunFinished):
        if chunk.record.content is not None:
            presentation.canonical_final_body_candidate = chunk.record.content
    elif isinstance(chunk, ToolStarted):
        presentation.start_tool(chunk.call, origin=chunk.origin)
    elif isinstance(chunk, ToolCompleted):
        presentation.complete_tool(chunk.call, origin=chunk.origin)
    elif isinstance(chunk, ToolProgress):
        presentation.show_tool_progress(chunk.tool_call_id, chunk.text)
    # Reasoning is shown only while a reply streams; a collected reply drops it.


async def collect_streamed_response_content(
    response_stream: AsyncIterator[StreamInputChunk],
    *,
    presentation: CollectedStreamPresentation,
    on_update: Callable[[], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolTraceEntry]]:
    """Collect a turn's stream into its presentation owner, retaining pending text and tool state.

    ``on_update`` runs after each chunk has been applied to ``presentation``. The stream is closed before any failure
    propagates, so its producer stops then.
    """
    try:
        async with closing_async_stream(response_stream):
            async for chunk in response_stream:
                _apply_collected_chunk(presentation, chunk)
                if on_update is not None:
                    await on_update()
    except ResponsePausedForApproval as error:
        # The collected presentation, a team's document state included, is what the approval card shows.
        error.capture_presentation(
            StreamingPresentation(
                response_text=presentation.final_text().rstrip(),
                tool_trace=tuple(deepcopy(presentation.tool_trace)),
                state=presentation.document_state,
            ),
        )
        raise

    return presentation.final_text(), presentation.tool_trace


def pause_without_approval_error(record: AnyRunRecord, *, entity_label: str) -> RuntimeError:
    """Return the error for a pause with no approval to ask for and no delegation to drive.

    Only the delegate tools run outside the loop, and the delegation driver resumes a run paused on them; a run
    paused on any other externally executed call has nothing that can resume it, so the reply fails loudly. The
    loop stored the ``PAUSED`` row; the reply's lifecycle owner then records the failure over it.
    """
    pending = [item.tool_execution.tool_name for item in record.requirements if not item.is_resolved()]
    msg = f"{entity_label} run {record.run_id!r} paused on {pending} without an approval to ask for"
    return RuntimeError(msg)


def own_approved_calls(record: AnyRunRecord, calls: Sequence[ApprovalCall]) -> tuple[ApprovalCall, ...]:
    """Return the approved calls among ``calls`` that the paused run ``record`` asked about itself.

    Projected child calls belong to the child executor, so a run waiting on a child asked about none of its own.
    """
    if DelegationState.from_metadata(record.metadata).pending_child_id is not None:
        return ()
    return tuple(call for call in calls if call.approved)


def pending_approval_requirements(record: AnyRunRecord) -> list[RunRequirement]:
    """Return copies of the requirements the paused run ``record``'s approval cards ask about.

    A delegation driver that asked about calls the run does not hold, a delegation's gate or the calls of the child
    it waits on, lists them as its pending tools; otherwise the cards ask about the run's own waiting calls.
    """
    delegation = DelegationState.from_metadata(record.metadata)
    if delegation.pending_tools:
        return [RunRequirement(ToolExecution.from_dict(tool)) for tool in delegation.pending_tools]
    return deepcopy(record.requirements)


def paused_attempt_from_response(
    record: AnyRunRecord,
    *,
    toolkit_owners: Mapping[tuple[str, str], str],
) -> PausedAttempt | None:
    """Extract confirmation requirements from one paused run record, which names its run and session."""
    if record.status is not RunStatus.paused:
        return None
    if record.session_id is None:
        msg = f"Paused run {record.run_id!r} names no session"
        raise RuntimeError(msg)
    delegation = DelegationState.from_metadata(record.metadata)
    if not delegation.pending_tools:
        return _paused_attempt_from_pause(
            record.requirements,
            toolkit_owners=toolkit_owners,
            session_id=record.session_id,
            run_id=record.run_id,
        )
    requirements = pending_approval_requirements(record)
    if delegation.pending_child_id is not None:
        # A source that recorded no toolkit leaves its call unowned, as a missing key does.
        toolkit_owners = {
            (source.child.child_agent_name, tool.tool_name): source.toolkit_name
            for tool in (requirement.tool_execution for requirement in requirements)
            if (source := delegation.pending_tool_sources.get(tool.tool_call_id)) is not None
            and source.toolkit_name is not None
        }
    paused = _paused_attempt_from_pause(
        requirements,
        toolkit_owners=toolkit_owners,
        session_id=record.session_id,
        run_id=record.run_id,
    )
    if paused is None:
        return None
    return replace(
        paused,
        approval_agent_name=delegation.pending_agent_name,
        delegation_storage_bindings=delegation.storage_bindings,
    )


def _paused_attempt_from_pause(
    requirements: Sequence[RunRequirement],
    *,
    toolkit_owners: Mapping[tuple[str, str], str],
    session_id: str,
    run_id: str,
) -> PausedAttempt | None:
    """Build one restartable pause from the calls of a run that still wait for a confirmation."""
    pending = tuple(requirement for requirement in requirements if requirement.needs_confirmation)
    call_ids = [requirement.tool_execution.tool_call_id for requirement in pending]
    if len(call_ids) != len(set(call_ids)):
        msg = "Paused approval requirements contain duplicate tool-call IDs"
        raise RuntimeError(msg)
    if not pending:
        return None
    return PausedAttempt(session_id=session_id, run_id=run_id, requirements=pending, toolkit_owners=toolkit_owners)


@dataclass(frozen=True)
class HandledAttempt:
    """One attempt that failed: the reply that says so, which the driver yields once it recorded the failure."""

    reply: FailedReply
    # Builds the run metadata the attempt publishes, as ``CompletedAttempt.build_run_metadata`` does.
    build_run_metadata: Callable[[], dict[str, Any]] | None = None


@dataclass(frozen=True)
class SkippedAttempt:
    """One unapproved response attempt with no answer or successful-run effects; its run is no turn."""


StreamAttemptResolution = CompletedAttempt | PausedAttempt | HandledAttempt | SkippedAttempt


@dataclass(frozen=True)
class AttemptResolved:
    """Sentinel a streaming attempt yields last to report its resolution."""

    resolution: StreamAttemptResolution


@dataclass(frozen=True)
class TurnAdapter[ChunkT]:
    """Entity-specific callbacks for one response turn."""

    load_scope: Callable[[], ScopeSessionContext]
    run_attempt: Callable[
        [TurnRunState, TurnContinuationRunState],
        AsyncIterator[ChunkT | AttemptResolved],
    ]
    make_text_chunk: Callable[[str], ChunkT]
    # The current-turn user message the turn's record keeps, which an interrupted row stores.
    user_message: str
    # The reply to an error no attempt handled, its credentials redacted.
    unexpected_error_text: Callable[[Exception], str]


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


def _interrupted_run_metadata(ctx: ResponseTurnContext, run: TurnRunState) -> dict[str, Any]:
    """Keep the best Matrix metadata available when an attempt is interrupted, rebuilt when prepare had not finished."""
    if run.run_metadata is not None:
        return run.run_metadata
    # Prepare had not finished, so the turn consumed no unseen events yet.
    return build_matrix_run_metadata(
        ctx.reply_to_event_id,
        [],
        room_id=ctx.room_id,
        thread_id=ctx.thread_id,
        requester_id=ctx.requester_id,
        correlation_id=ctx.correlation_id,
        extra_metadata=deepcopy(ctx.matrix_run_metadata),
    )


async def _record_interrupted(
    ctx: ResponseTurnContext,
    adapter: TurnAdapter[Any],
    sinks: TurnSinks,
    run: TurnRunState,
    continuation: TurnContinuationRunState,
    status: RunStatus,
) -> None:
    """Record the turn cut short with its running attempt's partial output, and store it once in the turn's scope.

    A process shutdown records nothing: the turn stays pending, and the successor's replay continues its reply. A turn
    that already settled (completed, skipped, paused, or interrupted) is never recorded again.
    """
    if run.record.outcome != "pending" or current_task_is_process_shutdown():
        return
    snapshot = run.partial_snapshot()
    run.record.record_interrupted(
        # Between two attempts, such as a continuation's or an empty run's retry, the attempt about to start names the
        # turn, so the record does not replace a run an earlier attempt stored.
        run_id=snapshot.attempt_run_id or continuation.active_run_id or ctx.run_id,
        run_metadata=_interrupted_run_metadata(ctx, run),
        assistant_text=snapshot.assistant_text,
        completed_tools=(*run.prior_completed_tools, *snapshot.completed_tools),
        interrupted_tools=snapshot.interrupted_tools,
        original_status=status,
        turn_input=snapshot.turn_input if snapshot.turn_input is not None else run.turn_input,
    )
    if not sinks.persist_interrupted:
        return
    response_event_id = sinks.response_event_id() if sinks.response_event_id is not None else None
    # A further cancellation, such as the one the streamer sends 0.5 s after a Stop, waits for the record too, so the
    # reply settles, and the conversation's next turn reads history, only once it is stored.
    await run_blocking_until_complete(_persist_interrupted, ctx, adapter, run, response_event_id)


def _persist_interrupted(
    ctx: ResponseTurnContext,
    adapter: TurnAdapter[Any],
    run: TurnRunState,
    response_event_id: str | None,
) -> None:
    """Store the turn's interrupted record into the scope its runs are stored in, loading it when the turn had not."""
    record = run.record
    try:
        scope_context = run.scope_context or adapter.load_scope()
        persist_interrupted_replay_snapshot(
            storage=scope_context.storage,
            session=scope_context.stored_or_first_session(),
            run_id=record.run_id or str(uuid4()),
            snapshot=record.interrupted_snapshot(response_event_id=response_event_id),
        )
    except Exception:
        # A lost replay row is the lesser harm; the turn's own exit must still run.
        logger.exception("Failed to persist interrupted replay state", entity=ctx.entity_label)


async def _settle_failure(
    ctx: ResponseTurnContext,
    adapter: TurnAdapter[Any],
    sinks: TurnSinks,
    run: TurnRunState,
    continuation: TurnContinuationRunState,
    failure: HandledAttempt,
) -> FailedReply:
    """Record a failed turn as interrupted and report its failure to the caller; return its error reply."""
    _publish_run_metadata(sinks, failure.build_run_metadata)
    await _record_interrupted(ctx, adapter, sinks, run, continuation, RunStatus.error)
    if sinks.on_failed is not None:
        sinks.on_failed(failure)
    return failure.reply


def _settle_empty_run(
    ctx: ResponseTurnContext,
    run: TurnRunState,
    resolution: CompletedAttempt,
    *,
    continuation_count: int,
) -> bool:
    """Log the empty completed run, which was never stored as a turn; return whether one retry is granted.

    The one-shot retry borrows a continuation slot so the outer loop's iteration
    budget stays authoritative; a granted retry drops the spent attempt exactly
    like the continuation handoff.
    """
    logger.warning(
        "model_returned_empty_response",
        entity=ctx.entity_label,
        session_id=ctx.session_id,
        run_id=resolution.run_id,
        output_tokens=resolution.usage.tokens.output_tokens if resolution.usage is not None else None,
    )
    if run.empty_response_retried or continuation_count >= TURN_CONTINUATION_LIMIT:
        return False
    run.empty_response_retried = True
    _drop_spent_attempt(run, resolution)
    return True


def _drop_spent_attempt(run: TurnRunState, resolution: CompletedAttempt) -> None:
    """Drop the attempt a retry or a continuation follows, keeping its finished calls and its usage for the turn.

    A continuation's spent run is stored as a turn with the input it read, so the turn no longer holds it for an
    interrupted record; an empty run is no turn, so its retry still does.
    """
    run.prior_completed_tools = (*run.prior_completed_tools, *resolution.completed_tools)
    if not resolution.is_empty:
        run.turn_input = NO_TURN_INPUT
    if resolution.usage is not None:
        run.prior_usage = (*run.prior_usage, resolution.usage)
    run.attempt = None


async def _remove_history_of_redacted_events(
    ctx: ResponseTurnContext,
    scope_context: ScopeSessionContext,
) -> None:
    """Remove persisted history derived from events since redacted, before the history is used.

    This is the only cleanup after a redaction: every event this scope's history derives
    from is checked against the agent's recorded redactions each time a
    response opens it, which also covers a message no turn answered but a later turn read
    as context.
    """
    if ctx.redacted_history_events is None or scope_context.session is None:
        return
    removed_event_ids = await remove_history_of_redacted_events(
        scope_context.storage,
        scope_context.session,
        ctx.redacted_history_events,
    )
    if removed_event_ids:
        logger.info(
            "Removed history derived from redacted events",
            session_id=scope_context.session.session_id,
            history_scope=scope_context.scope.key,
            redacted_event_ids=removed_event_ids,
        )


async def _load_scope_session(
    ctx: ResponseTurnContext,
    adapter: TurnAdapter[Any],
    run: TurnRunState,
) -> ScopeSessionContext:
    """Load the turn's scope session off the event loop, without the history of events since redacted."""
    scope_context = await run_blocking_until_complete(adapter.load_scope)
    await _remove_history_of_redacted_events(ctx, scope_context)
    run.scope_context = scope_context
    set_consumption_storage(scope_context.storage_factory)
    return scope_context


def skip_unapproved_attempt(participation: ParticipationGate | None, *, reason: str) -> SkippedAttempt | None:
    """Resolve an unapproved completion or failure; its run, if any, was stored as no turn (``replays_as_turn``)."""
    if participation is None or not participation.decline(reason):
        return None
    return SkippedAttempt()


def is_empty_reply(record: AnyRunRecord) -> bool:
    """Whether ``record``'s run completed with no text and no tool call, so a team run's leader ran no member.

    The content holds every text the run's requests finished with.
    """
    return record.status is RunStatus.completed and not record.tools and not (record.content or "").strip()


def replays_as_turn(ctx: ResponseTurnContext, record: AnyRunRecord, *, is_empty: bool) -> bool:
    """Whether an attempt's terminal ``record`` is stored as a turn later prompts replay; any other keeps only its usage.

    ``is_empty`` says whether the attempt's run is an empty reply, as the adapter reads it (``is_empty_reply``), which
    its resolution reads the same. A team's member run is stored with its team run. A run the turn's participation
    gate has not approved, declined or failed before the decision, answered nothing; a cancelled one is stored, and the
    turn records what it showed. An empty reply outside a quiet turn would teach the model that ending a turn
    immediately is the expected continuation.
    """
    if record.parent_run_id is not None:
        return True
    if ctx.participation is not None and not ctx.participation.approved:
        return record.status is RunStatus.cancelled
    return ctx.allow_no_report_response or not is_empty


def _publish_run_metadata(sinks: TurnSinks, build: Callable[[], dict[str, Any]] | None) -> None:
    """Publish one attempt's run metadata to the caller's collector, building it only for a caller that collects it."""
    if sinks.run_metadata_collector is not None and build is not None:
        sinks.run_metadata_collector.update(build())


def _record_completed_turn(
    sinks: TurnSinks,
    run: TurnRunState,
    resolution: CompletedAttempt,
    settle: _CompletionSettle,
) -> None:
    """Publish the reply's run metadata, which the turn-ending attempt builds, record the turn, and hand the attempt over."""
    _publish_run_metadata(sinks, resolution.build_run_metadata)
    run.record.record_completed(
        run_id=resolution.attempt_run_id or resolution.run_id,
        run_metadata=run.run_metadata,
        assistant_text=settle.recorded_text,
        completed_tools=(*run.prior_completed_tools, *settle.recorded_tools),
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
    # Final text the turn's chunks did not stream: the empty-reply notice, a
    # continuation limit message, or an approval resume's restored text; the
    # driver emits it as a final chunk when non-empty.
    response_text: str = ""
    # A quiet turn whose output holds no answer reports nothing: the driver
    # emits an empty final text, which replaces a document a team's chunks
    # showed, so delivery has no text to post.
    reports_nothing: bool = False


def _settle_completed_attempt(
    ctx: ResponseTurnContext,
    run: TurnRunState,
    resolution: CompletedAttempt,
    continuation: TurnContinuationRunState,
    *,
    continuation_count: int,
) -> _CompletionSettle:
    """Settle one completed attempt into a record/deliver plan or a continuation."""
    if resolution.is_empty and ctx.allow_no_report_response:
        return _CompletionSettle(keep_going=False, continuation=continuation)
    if resolution.is_empty:
        if _settle_empty_run(ctx, run, resolution, continuation_count=continuation_count):
            return _CompletionSettle(keep_going=True, continuation=continuation.next_attempt())
        # The notice falls through: run metadata and recorder completion
        # still apply to this notice-only turn.
        return _CompletionSettle(
            keep_going=False,
            continuation=continuation,
            response_text=ai_runtime.EMPTY_RESPONSE_NOTICE,
        )
    decision = continuation_decision_from_tools(
        (*stopped_calls(resolution.tool_executions), *resolution.control_calls),
        original_prompt=continuation.original_prompt,
        continuation_count=continuation_count,
    )
    if decision.continuation is not None:
        _drop_spent_attempt(run, resolution)
        return _CompletionSettle(
            keep_going=True,
            continuation=continuation.advance(
                continuation_prompt=decision.continuation.prompt,
                active_model_name=decision.continuation.model_name,
            ),
        )
    recorded_text = resolution.replayable_text
    response_text = resolution.response_text
    if decision.limit_message is not None:
        if not resolution.has_visible_content:
            recorded_text = decision.limit_message
            response_text = decision.limit_message
    elif ctx.allow_no_report_response and not resolution.replayable_text.strip():
        # An approval resume hands its final text over here, where a live attempt
        # already streamed it. Tool presentation and fallback text are not
        # semantic prose: the tool records remain part of the completed turn,
        # but quiet delivery has no final assistant body to publish.
        return _CompletionSettle(
            keep_going=False,
            continuation=continuation,
            recorded_text=recorded_text,
            recorded_tools=resolution.completed_tools,
            reports_nothing=True,
        )
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
    adapter: TurnAdapter[ChunkT],
    sinks: TurnSinks,
    *,
    continuation: TurnContinuationRunState,
    resume: TurnResume | None = None,
) -> AsyncGenerator[ChunkT | FailedReply]:
    """Own the whole response while binding context only during pulls and close; a failed turn ends with its reply."""
    lifetime = CliTurnLifetime()
    stream = context_bound_async_stream(
        context_factory=lifetime.bind,
        stream_factory=lambda: _stream_response_turn(
            ctx,
            adapter,
            sinks,
            continuation=continuation,
            cli_lifetime=lifetime,
            resume=resume or TurnResume(),
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
    adapter: TurnAdapter[ChunkT],
    sinks: TurnSinks,
    *,
    continuation: TurnContinuationRunState,
    cli_lifetime: CliTurnLifetime,
    resume: TurnResume,
) -> AsyncGenerator[ChunkT | FailedReply]:
    """Run one response turn, yielding the attempt chunks as they arrive.

    The driver is the only writer of the turn's record: it settles the record before it yields the text that ends the
    turn, so a consumer that closes the stream right after cannot leave it unsettled, and a consumer that closes it
    earlier (a Stop that lands in the streamer, a delivery failure) leaves the turn recorded as stopped.
    """
    run = TurnRunState(record=sinks.turn_recorder or TurnRecorder(), prior_usage=resume.prior_usage)
    run.record.start(
        user_message=adapter.user_message,
        user_message_is_structured=ctx.current_prompt_is_structured,
        run_metadata=_interrupted_run_metadata(ctx, run),
    )
    resumed_attempt = resume.attempt
    try:
        scope_context = await _load_scope_session(ctx, adapter, run)
        for continuation_count in range(resume.continuation_count, TURN_CONTINUATION_LIMIT + 1):
            cli_lifetime.continuation_count = continuation_count
            resolution: StreamAttemptResolution | None = None
            keep_going = False
            try:
                if resumed_attempt is not None:
                    resolution = resumed_attempt
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
                    run.record.mark_skipped()
                    return
                if isinstance(resolution, PausedAttempt):
                    run.record.mark_suspended()
                    raise ResponsePausedForApproval(replace(resolution, continuation_count=continuation_count))
                if isinstance(resolution, HandledAttempt):
                    yield await _settle_failure(ctx, adapter, sinks, run, continuation, resolution)
                    return
                settle = _settle_completed_attempt(
                    ctx,
                    run,
                    resolution,
                    continuation,
                    continuation_count=continuation_count,
                )
                continuation = settle.continuation
                keep_going = settle.keep_going
                if not keep_going:
                    _record_completed_turn(sinks, run, resolution, settle)
                if settle.response_text or settle.reports_nothing:
                    yield adapter.make_text_chunk(settle.response_text)
            finally:
                await cli_lifetime.retire_attempt()
                await finalize_consumption()
            if not keep_going:
                return
        _raise_continuation_budget_exhausted()
    except (asyncio.CancelledError, GeneratorExit):
        await _record_interrupted(ctx, adapter, sinks, run, continuation, RunStatus.cancelled)
        raise
    except ResponsePausedForApproval:
        raise
    except Exception as e:
        if skip_unapproved_attempt(ctx.participation, reason="run_failed_before_decision"):
            # A failure before the participation decision stays quiet: the turn is skipped.
            run.record.mark_skipped()
            logger.exception("Response turn skipped before participation", entity=ctx.entity_label)
            return
        failure = HandledAttempt(FailedReply(adapter.unexpected_error_text(e), e))
        reply = await _settle_failure(ctx, adapter, sinks, run, continuation, failure)
        logger.exception("Response turn failed", entity=ctx.entity_label)
        yield reply
