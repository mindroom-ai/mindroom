"""Team replies on the owned runtime: build the leader and its members, run the team, render the reply, resume a pause.

``team_response`` and ``team_response_stream`` are the team envelopes the response runner drives through the shared
turn drivers; both consume one ``run_team`` event stream into a ``TeamStreamPresentation``, so the blocking reply
and the streamed document render the same body. ``continue_paused_team_run`` rebuilds the team from a saved
approval and resumes its stored run through ``continue_team``.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom import ai_runtime
from mindroom.agent_loop.build import prompt_for_turn, tool_owners_by_agent
from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.output_limit import output_limit_policy
from mindroom.agent_loop.prompt import model_note_for
from mindroom.agent_loop.spec import AgentSpec, RunContext
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.agents import team_background_jobs
from mindroom.ai import run_delegated_child_response
from mindroom.ai_run_metadata import build_ai_run_metadata_content, build_prepared_history_metadata_content
from mindroom.approval.receipt import approval_receipt_hook
from mindroom.approval.resume import ResumeDecisions, approval_decisions, forward_resume
from mindroom.approval.tool_owners import required_approval_tool_names
from mindroom.constants import MATRIX_SEEN_EVENT_IDS_METADATA_KEY, is_silent_schedule_no_report_response
from mindroom.delegation.execution import DELEGATION_TOOL_NAMES, drive_delegation_stream, has_delegation_state
from mindroom.entity_resolution import entity_identity_registry
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import (
    ThreadHistoryRenderLimits,
    prepare_bound_team_run_context,
    render_prepared_team_messages_text,
)
from mindroom.helper_usage import helper_usage_context
from mindroom.history.mid_turn_compaction import mid_turn_compaction_hook, without_fold_records
from mindroom.history.native import restore_native_history
from mindroom.history.replay import configured_replay_plan
from mindroom.history.request_messages import build_request, build_resume_request, replayed_history
from mindroom.history.runtime import note_prepared_history_timing
from mindroom.history.session_context import (
    open_bound_scope_session_context,
    open_resolved_scope_session_context,
    resolve_bound_team_scope_context,
)
from mindroom.history.storage import new_scope_session, update_scope_seen_event_ids
from mindroom.history.team_scope import ad_hoc_team_scope_id
from mindroom.history.types import HistoryScope, PreparedHistoryState
from mindroom.hooks import render_enrichment_block, render_system_enrichment_block, render_transient_context
from mindroom.llm.events import ContentDelta, RunEvent, RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.llm_request_logging import (
    bind_llm_request_log_context,
    build_llm_request_log_context,
    stream_with_llm_request_log_context,
)
from mindroom.logging_config import get_logger
from mindroom.loop_hooks import agent_loop_hooks
from mindroom.media_inputs import MediaInputs
from mindroom.metadata_merge import deep_merge_metadata
from mindroom.providers.pipeline.request_log import provider_model_params
from mindroom.providers.registry import build_model_provider
from mindroom.response_turn import (
    AttemptResolved,
    BlockingAttemptResolution,
    BlockingTurnAdapter,
    CompletedApprovalRun,
    CompletedAttempt,
    ExcludedAttempt,
    HandledAttempt,
    PausedAttempt,
    ResponseTurnContext,
    StreamingTurnAdapter,
    TurnContinuationRunState,
    TurnPartialSnapshot,
    TurnSinks,
    build_matrix_run_metadata,
    pause_without_approval_error,
    paused_attempt_from_response,
    run_blocking_response_turn,
    stream_response_turn,
)
from mindroom.session_storage.agent_storage import runs_without
from mindroom.session_storage.store import persist_to
from mindroom.system_prompt import render_date_context
from mindroom.teams.members import (
    ResolvedExactTeamMembers,
    allow_direct_private_team_agents,
    materialize_exact_team_members,
    materialize_team_members,
    requested_team_agent_names,
    resolve_team_member_model_names,
)
from mindroom.teams.presentation import (
    TeamStreamPresentation,
    apply_team_event,
    format_team_header,
    render_team_member_slots,
)
from mindroom.teams.prompt import render_leader_system_message
from mindroom.teams.runtime import (
    MemberEvent,
    MemberRunEnded,
    continue_team,
    leader_tool_specs,
    run_team,
    team_usage,
)
from mindroom.teams.spec import TeamMember, TeamMode, TeamSpec
from mindroom.timing import emit_timing_event
from mindroom.tool_jobs.consumption import finalize_consumption, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.notices import append_turn_start_job_note
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.context_bound_streams import closing_async_stream, context_bound_async_stream
from mindroom.tool_system.events import StructuredStreamChunk
from mindroom.tool_system.runtime_context import get_tool_runtime_context, tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.run import ApprovedCall, Persist
    from mindroom.approval.resume import ResumeEvents
    from mindroom.config.main import Config, ResolvedRuntimeModel
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalCall
    from mindroom.history.request_messages import TurnMessages
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.history.types import CompactionLifecycle, ResolvedHistorySettings, ResolvedReplayPlan, ScopeSummary
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.utils import KnowledgeAvailabilityDetail
    from mindroom.llm.runs import AnyRunRecord, RunRecord, RunRequirement, ToolExecution
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.response_turn import EmptyRunDiscard, TurnRunState
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.streaming import ProgressPublisher
    from mindroom.teams.runtime import TeamCallKey, TeamRunEvent
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.events import StreamingToolTracker, ToolTraceEntry
    from mindroom.tool_system.invocation import ToolCall, ToolMiddleware, ToolNext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)

# Message length limits for team context and logging
_MAX_CONTEXT_MESSAGE_LENGTH = 200  # Maximum length for messages to include in thread context
_MAX_LOG_MESSAGE_LENGTH = 500  # Maximum length for messages in team response logs
_NO_TEAM_RESPONSE = "No team response generated."
_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS = ThreadHistoryRenderLimits(
    max_messages=30,
    max_message_length=_MAX_CONTEXT_MESSAGE_LENGTH,
    missing_sender_label="Unknown",
)
type TeamStreamChunk = str | StructuredStreamChunk


def _team_run_input_text(run_input: Sequence[Message]) -> str:
    return render_prepared_team_messages_text(run_input)


def _team_request_log_context(
    ctx: ResponseTurnContext,
    *,
    team_name: str,
    prompt: str,
    run_input: Sequence[Message],
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    return build_llm_request_log_context(
        agent_id=team_name,
        session_id=ctx.session_id or "",
        room_id=ctx.room_id,
        thread_id=ctx.thread_id,
        reply_to_event_id=ctx.reply_to_event_id,
        requester_id=ctx.requester_id,
        correlation_id=ctx.correlation_id,
        prompt=prompt,
        model_prompt=None,
        full_prompt=_team_run_input_text(run_input),
        metadata=metadata,
    )


def team_label(member_names: Sequence[str]) -> str:
    """Return the stored ``team_name`` of a team of these members, as Agno's team instances were named."""
    return f"Team-{'-'.join(member_names)}"


# ---------------------------------------------------------------------------
# Building the team
# ---------------------------------------------------------------------------


def _appended_context(existing: str, block: str) -> str:
    if not block:
        return existing
    stripped = existing.strip()
    return f"{stripped}\n\n{block}" if stripped else block


def _team_member(
    build: AgentBuild,
    system_enrichment: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str,
) -> TeamMember:
    """Return one member as the team runs it: this turn's system message, the roster role, and its loop hooks.

    A member runs under the agent hook policy: its own tool-call policy, output-limit policy, portable tool search,
    mid-turn compaction, and approval receipt, and its tools run under its own identity. It replays no history, so
    its compaction stays within the run, and the leader owns the queued-message notices of the turn. ``model_name``
    is the member's configured model for this turn, whose window its compaction sizes against. The compaction and
    receipt hooks hold the state of the run they serve, so each run or resume builds its own.
    """
    as_member = replace(build, tool_middleware=(*build.tool_middleware, _as_member(build.spec.id, model_name)))
    return TeamMember(
        spec=replace(build.spec, system_message=prompt_for_turn(build, system_enrichment)),
        role=build.prompt.role or "",
        hooks=partial(
            agent_loop_hooks,
            as_member,
            config=config,
            runtime_paths=runtime_paths,
            agent_name=build.spec.id,
            model_name=model_name,
            store=None,
            session=None,
            scope=None,
            scoped=False,
            native_compaction=None,
            prompt_id=None,
            queued_notices=False,
        ),
        request_template=build.request_template,
    )


def _as_member(member_id: str, model_name: str) -> ToolMiddleware:
    """Return the innermost step of a member's tool calls, which runs the tool under the member's own identity.

    The turn's tool runtime context names the team. Tools that act for the running agent, such as ``todo``'s default
    assignee and workspace templates or ``claude_agent``'s sessions and model, read it from that context, so each
    call reaches its entrypoint with the member's name and the model it runs on. The tool-call policy around the
    step still sees the turn's context, as it did when Agno ran the members.
    """

    async def run_as_member(call: ToolCall, call_next: ToolNext) -> object:
        context = get_tool_runtime_context()
        if context is None:
            return await call_next(call.arguments)
        with tool_runtime_context(replace(context, agent_name=member_id, active_model_name=model_name)):
            return await call_next(call.arguments)

    return run_as_member


def _resolve_team_instance_id(
    *,
    member_names: Sequence[str],
    config: Config,
    team_display_name: str,
    configured_team_name: str | None,
    scope_context: ScopeSessionContext | None,
    execution_identity: ToolExecutionIdentity | None,
) -> str:
    """Return the team id for scoped and no-session team runs."""
    if scope_context is not None:
        return scope_context.scope.scope_id
    bound_scope = resolve_bound_team_scope_context(
        member_names=member_names,
        config=config,
        team_name=configured_team_name,
        execution_identity=execution_identity,
    )
    if bound_scope is not None:
        return bound_scope.scope.scope_id
    return team_display_name


@dataclass(frozen=True)
class BuiltTeam:
    """One team ready to run: its spec and the tool-call policy the leader's own tools run under."""

    spec: TeamSpec
    leader_middleware: tuple[ToolMiddleware, ...]


def build_team_spec(
    *,
    members: ResolvedExactTeamMembers,
    mode: TeamMode,
    config: Config,
    runtime_paths: RuntimePaths,
    team_display_name: str,
    scope_context: ScopeSessionContext | None,
    execution_identity: ToolExecutionIdentity | None,
    model_name: str,
    configured_team_name: str | None,
    system_enrichment: str = "",
) -> BuiltTeam:
    """Build the team the leader loop runs: its model, prompt, job controls, call budget, and members.

    The leader prompt holds each member's roster role, the leader's tool instructions in offer order, the provider
    family's note, and the date context with ``system_enrichment`` appended; the team's own history settings and
    call budget come from the configured team, or the defaults for an ad hoc one.
    """
    model = build_model_provider(config, runtime_paths, model_name, execution_identity=execution_identity)
    team_view = config.resolve_entity(configured_team_name if configured_team_name in config.teams else None)
    team_id = _resolve_team_instance_id(
        member_names=members.requested_agent_names,
        config=config,
        team_display_name=team_display_name,
        configured_team_name=configured_team_name,
        scope_context=scope_context,
        execution_identity=execution_identity,
    )
    jobs = team_background_jobs(config, runtime_paths, execution_identity)
    leader_toolkits = () if jobs is None else (jobs.controls,)
    team_members = tuple(
        _team_member(
            build,
            system_enrichment,
            config=config,
            runtime_paths=runtime_paths,
            model_name=members.model_names[build.spec.id],
        )
        for build in members.builds
    )
    prepared = prepare_run_tools(
        AgentSpec(id=team_id, name=team_display_name, model=model, system_message=None, toolkits=leader_toolkits),
    )
    additional_context = _appended_context(
        render_date_context(config.timezone, datetime_context_template=config.get_prompt("DATETIME_CONTEXT_TEMPLATE")),
        system_enrichment,
    )
    system_message = render_leader_system_message(
        team_members,
        mode,
        tool_instructions=prepared.instructions,
        # The delegation tool is always offered, so every leader request offers tools.
        model_note=model_note_for(model.capabilities, offers_tools=True),
        additional_context=additional_context,
    )
    spec = TeamSpec(
        id=team_id,
        name=team_display_name,
        mode=mode,
        model=model,
        system_message=system_message,
        toolkits=leader_toolkits,
        tool_call_limit=team_view.max_tool_calls_per_turn,
        members=team_members,
    )
    return BuiltTeam(spec=spec, leader_middleware=() if jobs is None else (jobs.middleware,))


# ---------------------------------------------------------------------------
# Preparing a turn
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreparedTeamExecution:
    """One team turn's prepared input: the turn around its prompt, the history it replays, and its run metadata."""

    turn: TurnMessages
    history: tuple[Message, ...]
    summary: ScopeSummary | None
    run_metadata: dict[str, Any] | None
    unseen_event_ids: list[str]
    prepared_history: PreparedHistoryState
    # Configured model name resolved at preparation time. Run metadata must use
    # this snapshot instead of re-resolving, because the per-thread override
    # store can change mid-run (for example via the thread_model tool).
    runtime_model_name: str

    @property
    def prompt_text(self) -> str:
        """Return the prompt-visible text derived from the turn's messages."""
        return _team_run_input_text(self.turn.in_request_order())

    def run_input(self, media: MediaInputs) -> list[Message]:
        """Return one attempt's request body, media on the prompt: the summary, the replayed history, then the turn."""
        turn = deepcopy(self.turn)
        prompt = turn.prompt
        prompt.audio = media.audio or None
        prompt.images = media.images or None
        prompt.files = media.files or None
        prompt.videos = media.videos or None
        return build_request(system=(), summary=self.summary, history=deepcopy(self.history), turn=turn)


def _replayed_team_history(
    scope_context: ScopeSessionContext | None,
    prepared_history: PreparedHistoryState,
    *,
    config: Config,
    configured_team_name: str | None,
) -> tuple[list[Message], ScopeSummary | None]:
    """Return the stored team history this request replays under the resolved plan, and the scope summary."""
    if scope_context is None:
        return [], None
    return replayed_history(
        scope_context.session,
        scope_context.scope,
        _team_history_settings(config, configured_team_name),
        prepared_history.replay_plan,
    )


async def prepare_materialized_team_execution(
    ctx: ResponseTurnContext,
    *,
    scope_context: ScopeSessionContext | None,
    spec: TeamSpec,
    message: str,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    config: Config,
    runtime_paths: RuntimePaths,
    runtime_model: ResolvedRuntimeModel,
    response_sender_id: str | None,
    current_sender_id: str | None,
    configured_team_name: str | None,
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    current_event_id: str | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    thread_history_render_limits: ThreadHistoryRenderLimits | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> PreparedTeamExecution:
    """Prepare one built team's turn: its prompt and unseen context, the scope's history, and the run metadata."""
    ctx = replace(
        ctx,
        transient_enrichment_items=await append_turn_start_job_note(ctx.transient_enrichment_items),
    )
    transient_turn_context = render_transient_context(
        (render_enrichment_block(list(ctx.transient_enrichment_items)),),
    )
    prepared_execution = await prepare_bound_team_run_context(
        ctx,
        scope_context=scope_context,
        member_names=[member.spec.id for member in spec.members],
        leader_system_message=spec.system_message,
        leader_tools=leader_tool_specs(spec),
        model=spec.model,
        prompt=message,
        transient_context_messages=(
            (Message(role="user", content=transient_turn_context, add_to_agent_memory=False),)
            if transient_turn_context
            else ()
        ),
        thread_history=thread_history,
        runtime_paths=runtime_paths,
        config=config,
        entity_name=configured_team_name,
        active_model_name=runtime_model.model_name,
        active_context_window=runtime_model.context_window,
        response_sender_id=response_sender_id,
        current_sender_id=current_sender_id,
        current_timestamp_ms=current_timestamp_ms,
        current_event_id=current_event_id,
        current_prompt_is_structured=current_prompt_is_structured,
        compaction_lifecycle=compaction_lifecycle,
        thread_history_render_limits=thread_history_render_limits,
        pipeline_timing=pipeline_timing,
    )
    prepared_history = prepared_execution.prepared_history
    if pipeline_timing is not None:
        pipeline_timing.mark("history_ready")
        note_prepared_history_timing(pipeline_timing, prepared_history)
    history, summary = _replayed_team_history(
        scope_context,
        prepared_history,
        config=config,
        configured_team_name=configured_team_name,
    )
    run_extra_content = build_prepared_history_metadata_content(prepared_history)
    run_metadata = build_matrix_run_metadata(
        ctx.reply_to_event_id,
        prepared_execution.unseen_event_ids,
        room_id=ctx.room_id,
        thread_id=ctx.thread_id,
        requester_id=ctx.requester_id,
        correlation_id=ctx.correlation_id,
        model_params=provider_model_params(spec.model),
        extra_metadata=deep_merge_metadata(without_fold_records(ctx.matrix_run_metadata), run_extra_content),
    )
    return PreparedTeamExecution(
        turn=prepared_execution.turn,
        history=tuple(history),
        summary=summary,
        run_metadata=run_metadata,
        unseen_event_ids=prepared_execution.unseen_event_ids,
        prepared_history=prepared_history,
        runtime_model_name=runtime_model.model_name,
    )


# ---------------------------------------------------------------------------
# Storage, hooks, and bookkeeping shared by new runs and resumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TeamStorage:
    """Where one team run is stored: the scope's store and the session whose state its tools share."""

    store: ConversationStore
    session: ConversationSession
    scope: HistoryScope
    storage_factory: Callable[[], ConversationStore] | None


@dataclass
class _TerminalTeamRecord:
    """The terminal team record a run handed to its persist callback, however the run ended."""

    record: TeamRunRecord | None = None


def _team_storage(run: TurnRunState, *, session_id: str) -> _TeamStorage | None:
    """Return where one attempt's team run is stored, giving a scope without a stored session its first session.

    A team without a bound scope stores nothing, as Agno's team ran without a database then.
    """
    scope_context = run.scope_context
    if scope_context is None:
        return None
    session = scope_context.session
    if session is None:
        session = new_scope_session(session_id=session_id, scope_id=scope_context.scope.scope_id, is_team=True)
        run.scope_context = replace(scope_context, session=session)
    return _TeamStorage(scope_context.storage, session, scope_context.scope, scope_context.storage_factory)


def _persist_into(terminal: _TerminalTeamRecord, storage: _TeamStorage | None) -> Persist[AnyRunRecord]:
    """Return the persist callback of one team run: keep the team's terminal record, store every record, show it to the session."""
    store_persist = persist_to(storage.store, storage.session) if storage is not None else None

    async def persist(record: AnyRunRecord) -> None:
        if isinstance(record, TeamRunRecord):
            terminal.record = record
        if store_persist is None or storage is None:
            return
        await store_persist(record)
        session = storage.session
        session.runs = [*runs_without(session.runs, [record.run_id]), record]

    return persist


def _leader_hooks(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    entity_name: str | None,
    scope_id: str,
    spec: TeamSpec,
    runtime_model_name: str,
    storage: _TeamStorage | None,
    scoped: bool,
    prepared_history: PreparedHistoryState | None,
    prompt_id: str | None,
    compaction_lifecycle: CompactionLifecycle | None,
    turn_recorder: TurnRecorder | None,
    leader_middleware: tuple[ToolMiddleware, ...],
) -> LoopHooks:
    """Return the leader's hooks in the order the turn applies them: mid-turn compaction, then the approval receipt.

    The request cap is the loop's own; the queued-message notice follows every tool batch and a resume's entry, as
    for an agent's turn. A fold appends a summary row to the team scope only when this request replays it.
    """
    model = spec.model
    capabilities = model.capabilities
    native = prepared_history.native_history if prepared_history is not None else None
    notice_text = config.get_prompt("QUEUED_MESSAGE_NOTICE_TEXT")
    return LoopHooks(
        before_request=(
            mid_turn_compaction_hook(
                config=config,
                runtime_paths=runtime_paths,
                entity_name=entity_name if entity_name in config.teams else None,
                scope_id=scope_id,
                is_team=True,
                model_name=runtime_model_name,
                model=model,
                history_settings=_team_history_settings(config, entity_name),
                store=storage.store if storage is not None else None,
                session=storage.session if storage is not None and scoped else None,
                scope=storage.scope if storage is not None and scoped else None,
                native_compaction=native.native_compaction if native is not None else None,
                deferred_tool_names=frozenset(),
                prompt_id=prompt_id,
                lifecycle=compaction_lifecycle,
                turn_recorder=turn_recorder,
            ),
            approval_receipt_hook(capabilities),
        ),
        answer_tool_calls=output_limit_policy() if capabilities.claude_family else None,
        tool_middleware=leader_middleware,
        after_tool_batch=(ai_runtime.queued_notice_after_tool_batch(notice_text),),
        on_resume_entry=(ai_runtime.queued_notice_on_resume_entry(notice_text),),
    )


def _team_history_settings(config: Config, team_name: str | None) -> ResolvedHistorySettings:
    """Return the history settings of the configured team, or the defaults for an ad hoc one."""
    return config.resolve_entity(team_name if team_name in config.teams else None).history_settings


def _persist_bound_seen_event_ids(
    *,
    scope_context: ScopeSessionContext | None,
    session_id: str | None,
    event_ids: list[str],
) -> None:
    """Record the Matrix events this team turn consumed on the stored session row.

    The row is re-read and only then written, so the seen ids land without putting the loaded snapshot's fields
    back over what the run stored.
    """
    if not event_ids or scope_context is None or session_id is None:
        return
    session = scope_context.storage.load_session(session_id, "team")
    if session is None:
        session = new_scope_session(session_id=session_id, scope_id=scope_context.scope.scope_id, is_team=True)
    if update_scope_seen_event_ids(session, scope_context.scope, event_ids):
        scope_context.storage.save_session_fields(session, "metadata")


def _run_metadata_seen_event_ids(run_metadata: dict[str, Any] | None) -> list[str]:
    if not isinstance(run_metadata, dict):
        return []
    raw_event_ids = run_metadata.get(MATRIX_SEEN_EVENT_IDS_METADATA_KEY)
    if not isinstance(raw_event_ids, list):
        return []
    return [event_id for event_id in raw_event_ids if isinstance(event_id, str) and event_id]


def _register_team_notice_storage(
    *,
    scope_context: ScopeSessionContext | None,
    session_id: str | None,
    entity_name: str,
) -> None:
    """Register team storage for response-boundary notice finalization."""
    ai_runtime.register_queued_notice_storage(
        storage_factory=scope_context.storage_factory if scope_context is not None else None,
        session_id=session_id,
        session_kind="team",
        entity_name=entity_name,
    )


def _team_tool_executions(record: TeamRunRecord, members: Sequence[RunRecord]) -> list[ToolExecution]:
    """Return the leader's and every member's tool executions.

    Any member's completed model-switch call surfaces here and drives a whole-fan continuation, even when the team
    already produced visible output, as it does for an agent: gating on visible output would disable continuation
    in coordinate mode, where the leader almost always synthesizes text around a member's truncated tool call.
    """
    return [*(record.tools or ()), *(tool for member in members for tool in member.tools or ())]


# ---------------------------------------------------------------------------
# One attempt of a team turn
# ---------------------------------------------------------------------------


@dataclass
class _TeamAttempt:
    """One attempt of a team turn: its prepared input, its document, and what the run produced so far."""

    run_id: str
    members: ResolvedExactTeamMembers
    presentation: TeamStreamPresentation
    show_tool_calls: bool
    team_name: str
    runtime_model_name: str
    # The history preparation of a new run; a resume prepares none.
    prepared_history: PreparedHistoryState | None
    # The text each slot received, without tool markers: what the interrupted-turn record keeps.
    canonical_per_member: dict[str, str]
    canonical_consensus: str = ""
    member_records: list[RunRecord] = field(default_factory=list)
    terminal: _TerminalTeamRecord = field(default_factory=_TerminalTeamRecord)

    @property
    def tool_tracker(self) -> StreamingToolTracker:
        return self.presentation.tool_tracker

    def canonical_text(self) -> str:
        """Return the reply's text so far without display chrome: the member slots and the consensus."""
        return "\n\n".join(
            render_team_member_slots(
                self.presentation.member_ids,
                display_names_by_id=self.presentation.display_names_by_id,
                per_member=self.canonical_per_member,
                consensus=self.canonical_consensus,
            ),
        )

    def partial_snapshot(self) -> TurnPartialSnapshot:
        """Return the live partial-output view of this attempt, for a turn cancelled from outside."""
        return TurnPartialSnapshot(
            assistant_text=self.canonical_text(),
            completed_tools=tuple(self.tool_tracker.completed_tools),
            interrupted_tools=tuple(pending.trace_entry for pending in self.tool_tracker.pending_tools),
            attempt_run_id=self.run_id,
        )

    def apply(self, event: TeamRunEvent) -> bool:
        """Fold one run event into the attempt; return whether the document changed."""
        if isinstance(event, RunFinished):
            assert isinstance(event.record, TeamRunRecord)
            self.terminal.record = event.record
            return False
        if isinstance(event, MemberRunEnded):
            # A member resumed within this attempt ends twice; its later record replaces the paused one.
            self.member_records = [
                *(record for record in self.member_records if record.run_id != event.record.run_id),
                event.record,
            ]
            return False
        if isinstance(event, MemberEvent):
            inner = event.event
            if isinstance(inner, ContentDelta):
                member_id = self.presentation.resolve_member_id(event.member_id, event.member_name)
                self.canonical_per_member[member_id] += inner.text
            elif isinstance(inner, ToolStarted | ToolCompleted):
                self._emit_tool_timing(inner, tool_scope="member", agent_name=event.member_name)
            else:
                return False
        elif isinstance(event, ContentDelta):
            self.canonical_consensus += event.text
        elif isinstance(event, ToolStarted | ToolCompleted):
            self._emit_tool_timing(event, tool_scope="team", agent_name=None)
        else:
            return False
        apply_team_event(self.presentation, event)
        return True

    def _emit_tool_timing(
        self,
        event: ToolStarted | ToolCompleted,
        *,
        tool_scope: Literal["member", "team"],
        agent_name: str | None,
    ) -> None:
        emit_timing_event(
            "Dispatch tool-call timing",
            phase="agno_tool_call_started" if isinstance(event, ToolStarted) else "agno_tool_call_completed",
            team_name=self.team_name,
            tool_scope=tool_scope,
            agent_name=agent_name,
            tool_name=event.tool.tool_name,
            tool_call_id=event.tool.tool_call_id,
            show_tool_calls=self.show_tool_calls,
        )

    def chunk(self) -> StructuredStreamChunk | None:
        """Return the rendered document as a stream chunk, or None while it is still empty."""
        rendered_body = self.presentation.render_body()
        if not rendered_body:
            return None
        return StructuredStreamChunk(
            content=rendered_body,
            tool_trace=self.presentation.tool_trace.copy(),
            presentation_state=self.presentation.to_state(),
        )

    def metadata_content(self, config: Config, *, session_id: str | None, status: RunStatus) -> dict[str, Any]:
        """Return the run metadata payload: the leader's identity, the team's usage, and the prepared history."""
        record = self.terminal.record
        prepared_history = self.prepared_history
        return build_ai_run_metadata_content(
            config=config,
            model_name=self.runtime_model_name,
            run_id=record.run_id if record is not None else self.run_id,
            session_id=(record.session_id if record is not None else None) or session_id,
            status=status,
            model=record.model if record is not None else None,
            model_provider=record.model_provider if record is not None else None,
            metrics=team_usage(record, self.member_records) if record is not None else None,
            context_metrics=record.metrics if record is not None else None,
            context_input_tokens=prepared_history.prepared_context_tokens if prepared_history is not None else None,
            tool_count=len(_team_tool_executions(record, self.member_records)) if record is not None else 0,
            prepared_history=prepared_history,
        )


@dataclass
class _TeamTurnHolder:
    """Live per-turn team state shared between attempt closures and adapter callbacks."""

    team_members: ResolvedExactTeamMembers | None = None
    member_model_names: dict[str, str] = field(default_factory=dict)
    attempt: _TeamAttempt | None = None
    attempt_started: bool = False

    def partial_snapshot(self) -> TurnPartialSnapshot:
        """Return the live partial-output view, empty between a released attempt and the next."""
        return self.attempt.partial_snapshot() if self.attempt is not None else TurnPartialSnapshot()

    def release_attempt_members(self, _scope_context: ScopeSessionContext | None) -> None:
        """Drop the spent attempt's members so the next attempt rebuilds them with the switched model.

        Cancel snapshots taken between this release and the next attempt must not see the spent attempt's partials;
        its completed tools already carried over via the turn state.
        """
        if self.team_members is None:
            return
        self.team_members = None
        self.attempt = None


@dataclass(frozen=True)
class _TeamTurn:
    """The constants of one team turn, shared by its attempts."""

    ctx: ResponseTurnContext
    orchestrator: OrchestratorRuntime
    config: Config
    execution_identity: ToolExecutionIdentity | None
    mode: TeamMode
    requested_agent_names: list[str]
    thread_history: Sequence[ResolvedVisibleMessage] | None
    model_name: str | None
    media: MediaInputs
    show_tool_calls: bool
    run_id_callback: Callable[[str], None] | None
    user_id: str | None
    response_sender_id: str | None
    compaction_lifecycle: CompactionLifecycle | None
    run_metadata_collector: dict[str, Any] | None
    configured_team_name: str | None
    pipeline_timing: DispatchPipelineTiming | None
    attempt_model_runtime: ai_runtime.AttemptModelRuntime | None
    turn_recorder: TurnRecorder
    reason_prefix: str
    holder: _TeamTurnHolder

    @property
    def runtime_paths(self) -> RuntimePaths:
        return self.orchestrator.runtime_paths

    @property
    def entity_name(self) -> str:
        """The name the team's stored notices and logs carry: the configured team, or the materialized label."""
        return self.configured_team_name or self.ctx.entity_label

    @property
    def session_id(self) -> str:
        """The turn's session id; team turns always run with one."""
        assert self.ctx.session_id is not None
        return self.ctx.session_id

    async def members_for_attempt(self, continuation_state: TurnContinuationRunState) -> ResolvedExactTeamMembers:
        """Return the attempt's member set, rebuilding released members with the continuation's model."""
        holder = self.holder
        if continuation_state.apply_model_to_team_members and continuation_state.active_model_name is not None:
            holder.member_model_names = dict.fromkeys(self.requested_agent_names, continuation_state.active_model_name)
        members = holder.team_members
        if members is None:
            members = await asyncio.to_thread(
                materialize_team_members,
                self.requested_agent_names,
                self.orchestrator,
                self.execution_identity,
                reason_prefix=self.reason_prefix,
                configured_team_name=self.configured_team_name,
                active_model_names=holder.member_model_names,
            )
            holder.team_members = members
        return members

    async def start_attempt(
        self,
        run: TurnRunState,
        continuation_state: TurnContinuationRunState,
        *,
        stream: bool,
    ) -> tuple[_TeamAttempt, AsyncIterator[TeamRunEvent]]:
        """Build the attempt's team and prepared input, and return its state with the run's event stream."""
        members = await self.members_for_attempt(continuation_state)
        config = self.config
        # Resolve the runtime model once here and hand it down so the team spec and the run metadata cannot disagree.
        runtime_model = config.resolve_runtime_model(
            entity_name=self.configured_team_name,
            active_model_name=continuation_state.active_model_name or self.model_name,
            room_id=self.ctx.room_id,
            thread_id=self.ctx.thread_id,
            runtime_paths=self.runtime_paths,
        )
        built = build_team_spec(
            members=members,
            mode=self.mode,
            config=config,
            runtime_paths=self.runtime_paths,
            team_display_name=team_label(members.requested_agent_names),
            scope_context=run.scope_context,
            execution_identity=self.execution_identity,
            model_name=runtime_model.model_name,
            configured_team_name=self.configured_team_name,
            system_enrichment=render_system_enrichment_block(self.ctx.system_enrichment_items),
        )
        prepared = await prepare_materialized_team_execution(
            self.ctx,
            scope_context=run.scope_context,
            spec=built.spec,
            message=continuation_state.active_prompt,
            thread_history=self.thread_history,
            config=config,
            runtime_paths=self.runtime_paths,
            runtime_model=runtime_model,
            response_sender_id=self.response_sender_id,
            current_sender_id=self.ctx.current_sender_id or self.user_id,
            current_timestamp_ms=continuation_state.active_current_timestamp_ms,
            current_event_id=continuation_state.active_current_event_id,
            current_prompt_is_structured=continuation_state.active_current_prompt_is_structured,
            compaction_lifecycle=self.compaction_lifecycle,
            configured_team_name=self.configured_team_name,
            thread_history_render_limits=_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS,
            pipeline_timing=self.pipeline_timing,
        )
        run.unseen_event_ids = prepared.unseen_event_ids
        run.run_metadata = prepared.run_metadata
        self.turn_recorder.set_run_metadata(prepared.run_metadata)
        presentation = TeamStreamPresentation.new(
            members.requested_agent_names,
            members.display_names,
            show_tool_calls=self.show_tool_calls,
        )
        attempt = _TeamAttempt(
            run_id=continuation_state.active_run_id or str(uuid4()),
            members=members,
            presentation=presentation,
            show_tool_calls=self.show_tool_calls,
            team_name=self.entity_name,
            runtime_model_name=prepared.runtime_model_name,
            prepared_history=prepared.prepared_history,
            canonical_per_member=dict.fromkeys(presentation.member_ids, ""),
        )
        self.holder.attempt = attempt
        self.holder.attempt_started = True
        logger.info("executing_team_response", agent_count=len(members.builds), mode=self.mode.value)
        run_input = prepared.run_input(self.media)
        logger.info("team_prompt_preview", agents=members.display_names, prompt_preview=prepared.prompt_text[:500])
        storage = _team_storage(run, session_id=self.session_id)
        scoped = (
            storage is not None
            and prepared.prepared_history.replay_plan is not None
            and prepared.prepared_history.replay_plan.add_history_to_context
        )
        hooks = _leader_hooks(
            config=config,
            runtime_paths=self.runtime_paths,
            entity_name=self.configured_team_name,
            scope_id=built.spec.id,
            spec=built.spec,
            runtime_model_name=prepared.runtime_model_name,
            storage=storage,
            scoped=scoped,
            prepared_history=prepared.prepared_history,
            prompt_id=prepared.turn.prompt.id,
            compaction_lifecycle=self.compaction_lifecycle,
            turn_recorder=self.turn_recorder,
            leader_middleware=built.leader_middleware,
        )
        native = prepared.prepared_history.native_history
        ai_runtime.note_attempt_run_id(self.run_id_callback, attempt.run_id)
        request_context = _team_request_log_context(
            self.ctx,
            team_name=self.entity_name,
            prompt=continuation_state.active_prompt,
            run_input=run_input,
            metadata=prepared.run_metadata,
        )
        request_template = ModelRequest(
            messages=(),
            stream=stream,
            native_compaction=native.native_compaction if native is not None else None,
            portable_replay=native.portable_replay if native is not None else False,
        )
        with bind_llm_request_log_context(**request_context):
            if self.pipeline_timing is not None:
                self.pipeline_timing.mark_model_request()
            events = run_team(
                built.spec,
                run_input,
                ctx=RunContext(
                    run_id=attempt.run_id,
                    session_id=self.session_id,
                    user_id=self.user_id,
                    metadata=prepared.run_metadata,
                    session_state=storage.session.session_state if storage is not None else {},
                ),
                hooks=hooks,
                persist=_persist_into(attempt.terminal, storage),
                request_template=request_template,
            )
        events = _under_team_scope(events, storage.scope if storage is not None else None)
        if storage is not None:
            events = _drive_team_delegations(
                events,
                _TeamResume(
                    spec=built.spec,
                    storage=storage,
                    terminal=attempt.terminal,
                    hooks=hooks,
                    # A delegation continuation streams its requests even in a blocking turn, as Agno's did.
                    request_template=replace(request_template, stream=True),
                    settings=_team_history_settings(config, self.configured_team_name),
                    plan=prepared.prepared_history.replay_plan,
                    user_id=self.user_id,
                    toolkit_owners=tool_owners_by_agent(*members.builds),
                ),
                owners={},
                team_name=self.entity_name,
                config=config,
                runtime_paths=self.runtime_paths,
                execution_identity=self.execution_identity,
                refresh_scheduler=self.orchestrator.knowledge_refresh_scheduler,
            )
        return attempt, ai_runtime.stream_attempt_with_model(
            self.attempt_model_runtime,
            stream_with_llm_request_log_context(events, request_context=request_context),
            active_model_name=prepared.runtime_model_name,
        )

    async def run_blocking_attempt(
        self,
        run: TurnRunState,
        continuation_state: TurnContinuationRunState,
    ) -> BlockingAttemptResolution:
        """Run one prepared team attempt and collect its events into the reply."""
        attempt, events = await self.start_attempt(run, continuation_state, stream=False)
        try:
            async with closing_async_stream(events):
                async for event in events:
                    if attempt.apply(event):
                        self.sync_live_turn_recorder(run, attempt)
        except Exception as error:
            logger.exception("team_response_failed", agents=attempt.members.display_names)
            return ExcludedAttempt(
                original_status=RunStatus.error,
                response_text=self.error_text(error),
                reason=str(error),
                partial_text=attempt.canonical_text(),
                completed_tools=tuple(attempt.tool_tracker.completed_tools),
                interrupted_tools=tuple(pending.trace_entry for pending in attempt.tool_tracker.pending_tools),
                session_id=self.session_id,
                run_id=attempt.run_id,
                metadata_content=self.failure_metadata(attempt),
            )
        record = self.terminal_record(attempt)
        if record.status is RunStatus.paused:
            return self.resolve_paused(attempt, record)
        return self.resolve_completed(run, attempt, record, blocking=True)

    async def run_streaming_attempt(
        self,
        run: TurnRunState,
        continuation_state: TurnContinuationRunState,
    ) -> AsyncGenerator[TeamStreamChunk | AttemptResolved, None]:
        """Stream one team attempt as its document grows, ending with its ``AttemptResolved`` sentinel."""
        attempt, events = await self.start_attempt(run, continuation_state, stream=True)
        try:
            async with closing_async_stream(events):
                async for event in events:
                    if not attempt.apply(event):
                        continue
                    self.sync_live_turn_recorder(run, attempt)
                    chunk = attempt.chunk()
                    if chunk is not None:
                        yield chunk
        except Exception as error:
            logger.exception("team_streaming_failed", agents=attempt.members.display_names)
            self.sync_live_turn_recorder(run, attempt)
            yield self.error_text(error)
            yield AttemptResolved(HandledAttempt(metadata_content=self.failure_metadata(attempt)))
            return
        record = self.terminal_record(attempt)
        if record.status is RunStatus.paused:
            paused = self.resolve_paused(attempt, record)
            if paused.response_text:
                yield StructuredStreamChunk(
                    content=paused.response_text,
                    tool_trace=list(paused.tool_trace),
                    presentation_state=paused.response_presentation_state,
                )
            yield AttemptResolved(paused)
            return
        yield AttemptResolved(self.resolve_completed(run, attempt, record, blocking=False))

    def terminal_record(self, attempt: _TeamAttempt) -> TeamRunRecord:
        """Return the record the attempt's run ended with; the loop always hands one over before it finishes."""
        record = attempt.terminal.record
        if record is None:
            msg = f"team run {attempt.run_id!r} ended without its terminal record"
            raise RuntimeError(msg)
        return record

    def failure_metadata(self, attempt: _TeamAttempt) -> dict[str, Any] | None:
        """Return the run metadata of a failed attempt when the caller collects metadata and the run was stored."""
        if self.run_metadata_collector is None or attempt.terminal.record is None:
            return None
        return attempt.metadata_content(self.config, session_id=self.session_id, status=RunStatus.error)

    async def finalize_attempt(self, scope_context: ScopeSessionContext | None) -> None:
        """Register the team's storage for the turn's queued-notice finalization once an attempt ran."""
        if not self.holder.attempt_started:
            return
        self.holder.attempt_started = False
        self.register_notice_storage(scope_context)

    def sync_live_turn_recorder(self, run: TurnRunState, attempt: _TeamAttempt) -> None:
        """Refresh the live turn state from the attempt's document, so an interruption records what was shown."""
        run.turn_state.sync_partial(
            self.turn_recorder,
            run_metadata=run.run_metadata,
            assistant_text=attempt.canonical_text(),
            completed_tools=attempt.tool_tracker.completed_tools,
            interrupted_tools=[pending.trace_entry for pending in attempt.tool_tracker.pending_tools],
        )

    def resolve_paused(self, attempt: _TeamAttempt, record: TeamRunRecord) -> PausedAttempt:
        """Return the pause the lifecycle owner asks the requester about, rendered into the attempt's document."""
        paused = paused_attempt_from_response(
            record,
            fallback_session_id=self.ctx.session_id,
            fallback_run_id=attempt.run_id,
            toolkit_owners=tool_owners_by_agent(*attempt.members.builds),
        )
        if paused is None:
            raise pause_without_approval_error(record, entity_label="team")
        return replace(
            _continued_team_pause(attempt.presentation, paused),
            runtime_model_name=attempt.runtime_model_name,
            team_member_model_names=tuple(sorted(self.holder.member_model_names.items())),
        )

    def resolve_completed(
        self,
        run: TurnRunState,
        attempt: _TeamAttempt,
        record: TeamRunRecord,
        *,
        blocking: bool,
    ) -> CompletedAttempt:
        """Return the completed attempt: its rendered body, what replays, and the turn's metadata."""
        if self.ctx.reply_to_event_id:
            _persist_bound_seen_event_ids(
                scope_context=run.scope_context,
                session_id=self.ctx.session_id,
                event_ids=_run_metadata_seen_event_ids(run.run_metadata),
            )
        tool_executions = _team_tool_executions(record, attempt.member_records)
        canonical_text = attempt.canonical_text()
        has_visible_output = bool(canonical_text)
        header = format_team_header(attempt.members.display_names)
        rendered = attempt.presentation.render_body() or header + _NO_TEAM_RESPONSE
        logger.info(
            "team_response_preview",
            agents=attempt.members.display_names,
            response_preview=rendered[:_MAX_LOG_MESSAGE_LENGTH],
        )
        if len(rendered) > _MAX_LOG_MESSAGE_LENGTH:
            logger.debug("team_response_full", agents=attempt.members.display_names, response=rendered)
        response_text = ""
        if blocking:
            consensus = attempt.canonical_consensus
            silent = (
                self.ctx.allow_no_report_response
                and is_silent_schedule_no_report_response(consensus)
                and not any(text.strip() for text in attempt.canonical_per_member.values())
            )
            response_text = consensus.strip() if silent else rendered
        metadata_content = (
            attempt.metadata_content(self.config, session_id=self.ctx.session_id, status=record.status)
            if self.run_metadata_collector is not None
            else None
        )
        return CompletedAttempt(
            response_text=response_text,
            # The "No team response generated." fallback is display chrome, not replayable assistant text.
            replayable_text=header + canonical_text if has_visible_output else "",
            has_visible_content=has_visible_output,
            is_empty=record.status is RunStatus.completed and not tool_executions and not has_visible_output,
            session_id=record.session_id,
            run_id=record.run_id,
            attempt_run_id=attempt.run_id,
            runtime_model_name=attempt.runtime_model_name,
            output_tokens=record.metrics.output_tokens if record.metrics is not None else None,
            tool_executions=tuple(tool_executions),
            completed_tools=tuple(attempt.tool_tracker.completed_tools),
            metadata_content=metadata_content,
        )

    def error_text(self, error: Exception) -> str:
        return get_user_friendly_error_message(error, self.ctx.entity_label, runtime_paths=self.runtime_paths)

    def register_notice_storage(self, scope_context: ScopeSessionContext | None) -> None:
        _register_team_notice_storage(
            scope_context=scope_context,
            session_id=self.ctx.session_id,
            entity_name=self.entity_name,
        )

    def discard_empty_run(self, scope_context: ScopeSessionContext | None, discard: EmptyRunDiscard) -> None:
        resolved_session_id = _resolved_team_session_id(scope_context, discard.session_id or self.ctx.session_id)
        if resolved_session_id is None:
            return
        ai_runtime.discard_empty_completed_run(
            scope_context=scope_context,
            session_id=resolved_session_id,
            run_id=discard.run_id,
            entity_name=self.entity_name,
            output_tokens=discard.output_tokens,
        )

    def open_scope(self) -> AbstractContextManager[ScopeSessionContext | None]:
        return open_bound_scope_session_context(
            member_names=self.requested_agent_names,
            session_id=self.ctx.session_id,
            runtime_paths=self.runtime_paths,
            config=self.config,
            execution_identity=self.execution_identity,
            team_name=self.configured_team_name,
        )

    def initial_continuation(
        self,
        message: str,
        *,
        current_timestamp_ms: float | None,
        current_prompt_is_structured: bool,
    ) -> TurnContinuationRunState:
        return TurnContinuationRunState.initial(
            prompt=message,
            model_prompt=None,
            current_timestamp_ms=current_timestamp_ms,
            current_prompt_is_structured=current_prompt_is_structured,
            current_event_id=self.ctx.reply_to_event_id,
            run_id=self.ctx.run_id,
            continuation_model_prompt_tail="",
        )


def _under_team_scope(
    events: AsyncIterator[TeamRunEvent],
    scope: HistoryScope | None,
) -> AsyncIterator[TeamRunEvent]:
    """Pull the run's events with the team scope bound as the scope the running entity stores to.

    Tools that address history by scope, such as ``compact_context``, read it from the tool runtime context, so a
    member compacts the team's history, not its own agent's. The binding holds only while an event is pulled, so
    the consumer never runs under it, and it is read at each pull, so the bindings around it, such as the attempt
    model's, reach the run.
    """

    def with_scope() -> AbstractContextManager[None]:
        context = get_tool_runtime_context()
        bound = replace(context, history_scope=scope) if context is not None and scope is not None else context
        return tool_runtime_context(bound)

    return context_bound_async_stream(context_factory=with_scope, stream_factory=lambda: events)


def _resolved_team_session_id(scope_context: ScopeSessionContext | None, session_id: str | None) -> str | None:
    """Return the effective team session id, falling back to the opened scope session."""
    if session_id is not None:
        return session_id
    if scope_context is not None and scope_context.session is not None:
        return scope_context.session.session_id
    return None


def _continued_team_pause(presentation: TeamStreamPresentation, paused: PausedAttempt) -> PausedAttempt:
    """Render the pending calls into their slots and freeze the document the pause shows."""
    for requirement in paused.requirements:
        scope = (
            f"agent:{presentation.resolve_member_id(requirement.member_agent_id, requirement.member_agent_name)}"
            if requirement.member_agent_id is not None
            else "team"
        )
        presentation.start_tool(scope, requirement.tool_execution)
    return replace(
        paused,
        response_text=presentation.render_body(),
        tool_trace=tuple(presentation.tool_trace),
        response_presentation_state=presentation.to_state(),
    )


def _team_turn(
    ctx: ResponseTurnContext,
    *,
    orchestrator: OrchestratorRuntime,
    execution_identity: ToolExecutionIdentity | None,
    mode: TeamMode,
    requested_agent_names: list[str],
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    model_name: str | None,
    media: MediaInputs | None,
    show_tool_calls: bool,
    run_id_callback: Callable[[str], None] | None,
    user_id: str | None,
    response_sender_id: str | None,
    compaction_lifecycle: CompactionLifecycle | None,
    run_metadata_collector: dict[str, Any] | None,
    configured_team_name: str | None,
    pipeline_timing: DispatchPipelineTiming | None,
    attempt_model_runtime: ai_runtime.AttemptModelRuntime | None,
    turn_recorder: TurnRecorder,
    reason_prefix: str,
    member_model_names: Mapping[str, str] | None,
) -> _TeamTurn | str:
    """Materialize the team's members and return the turn's constants, or the text that explains why it cannot run."""
    assert orchestrator.config is not None
    if not ctx.session_id:
        msg = "team response turns require ResponseTurnContext.session_id"
        raise ValueError(msg)
    config = orchestrator.config
    config.assert_team_agents_supported(
        requested_agent_names,
        allow_direct_private_agents=allow_direct_private_team_agents(
            execution_identity,
            configured_team_name=configured_team_name,
        ),
    )
    active_member_model_names = (
        dict(member_model_names)
        if member_model_names is not None
        else resolve_team_member_model_names(
            requested_agent_names,
            ctx.room_id,
            config,
            orchestrator.runtime_paths,
            thread_id=ctx.thread_id,
        )
    )
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
    try:
        # Member agent builds walk the filesystem (workspace scaffolding,
        # context files, session storage); keep them off the event loop (#1260).
        team_members = materialize_team_members(
            requested_agent_names,
            orchestrator,
            execution_identity,
            unavailable_bases=unavailable_bases,
            reason_prefix=reason_prefix,
            configured_team_name=configured_team_name,
            active_model_names=active_member_model_names,
        )
    except ValueError as exc:
        return str(exc)
    return _TeamTurn(
        ctx=replace(
            ctx,
            entity_label=f"Team ({', '.join(team_members.display_names)})",
            background_tool_jobs=background_tool_jobs_enabled(config, orchestrator.runtime_paths),
            transient_enrichment_items=append_knowledge_availability_enrichment(
                ctx.transient_enrichment_items,
                unavailable_bases,
            ),
        ),
        orchestrator=orchestrator,
        config=config,
        execution_identity=execution_identity,
        mode=mode,
        requested_agent_names=requested_agent_names,
        thread_history=thread_history,
        model_name=model_name,
        media=media or MediaInputs(),
        show_tool_calls=show_tool_calls,
        run_id_callback=run_id_callback,
        user_id=user_id,
        response_sender_id=response_sender_id,
        compaction_lifecycle=compaction_lifecycle,
        run_metadata_collector=run_metadata_collector,
        configured_team_name=configured_team_name,
        pipeline_timing=pipeline_timing,
        attempt_model_runtime=attempt_model_runtime,
        turn_recorder=turn_recorder,
        reason_prefix=reason_prefix,
        holder=_TeamTurnHolder(team_members=team_members, member_model_names=team_members.model_names),
    )


# ---------------------------------------------------------------------------
# The blocking and streaming envelopes
# ---------------------------------------------------------------------------


async def team_response(
    agent_names: list[str],
    mode: TeamMode,
    message: str,
    orchestrator: OrchestratorRuntime,
    execution_identity: ToolExecutionIdentity | None,
    ctx: ResponseTurnContext,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_name: str | None = None,
    media: MediaInputs | None = None,
    show_tool_calls: bool = True,
    run_id_callback: Callable[[str], None] | None = None,
    user_id: str | None = None,
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    response_sender_id: str | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    run_metadata_collector: dict[str, Any] | None = None,
    configured_team_name: str | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    attempt_model_runtime: ai_runtime.AttemptModelRuntime | None = None,
    *,
    turn_recorder: TurnRecorder,
    reason_prefix: str = "Team request",
    member_model_names: Mapping[str, str] | None = None,
) -> str:
    """Run one team turn to its final text.

    ``ctx`` carries the per-turn Matrix identity; this entry refines its ``entity_label`` to the materialized team
    label and appends the knowledge-availability enrichment before the turn runs.
    """
    turn = await asyncio.to_thread(
        _team_turn,
        ctx,
        orchestrator=orchestrator,
        execution_identity=execution_identity,
        mode=mode,
        requested_agent_names=requested_team_agent_names(agent_names),
        thread_history=thread_history,
        model_name=model_name,
        media=media,
        show_tool_calls=show_tool_calls,
        run_id_callback=run_id_callback,
        user_id=user_id,
        response_sender_id=response_sender_id,
        compaction_lifecycle=compaction_lifecycle,
        run_metadata_collector=run_metadata_collector,
        configured_team_name=configured_team_name,
        pipeline_timing=pipeline_timing,
        attempt_model_runtime=attempt_model_runtime,
        turn_recorder=turn_recorder,
        reason_prefix=reason_prefix,
        member_model_names=member_model_names,
    )
    if isinstance(turn, str):
        return turn

    adapter = BlockingTurnAdapter(
        open_scope=turn.open_scope,
        run_attempt=turn.run_blocking_attempt,
        snapshot_partial=turn.holder.partial_snapshot,
        release_attempt_entity=turn.holder.release_attempt_members,
        finalize_attempt=turn.finalize_attempt,
        unexpected_error_text=turn.error_text,
        discard_empty_run=turn.discard_empty_run,
    )
    return await run_blocking_response_turn(
        turn.ctx,
        adapter,
        TurnSinks(turn_recorder=turn_recorder, run_metadata_collector=run_metadata_collector),
        continuation=turn.initial_continuation(
            message,
            current_timestamp_ms=current_timestamp_ms,
            current_prompt_is_structured=current_prompt_is_structured,
        ),
    )


async def team_response_stream(
    agent_ids: list[MatrixID],
    message: str,
    orchestrator: OrchestratorRuntime,
    execution_identity: ToolExecutionIdentity | None,
    ctx: ResponseTurnContext,
    mode: TeamMode = TeamMode.COORDINATE,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_name: str | None = None,
    media: MediaInputs | None = None,
    show_tool_calls: bool = True,
    run_id_callback: Callable[[str], None] | None = None,
    user_id: str | None = None,
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    response_sender_id: str | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    run_metadata_collector: dict[str, Any] | None = None,
    configured_team_name: str | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    attempt_model_runtime: ai_runtime.AttemptModelRuntime | None = None,
    *,
    turn_recorder: TurnRecorder,
    reason_prefix: str = "Team request",
    member_model_names: Mapping[str, str] | None = None,
) -> AsyncIterator[TeamStreamChunk]:
    """Stream one team turn as a live document: a header, one section per member, and the leader's consensus.

    The document is rebuilt as events arrive so its final shape matches the blocking reply. ``ctx`` carries the
    per-turn Matrix identity; this entry refines its ``entity_label`` to the materialized team label and appends the
    knowledge-availability enrichment before the turn runs.
    """
    assert orchestrator.config is not None
    registry = orchestrator.config
    turn = await asyncio.to_thread(
        _team_turn,
        ctx,
        orchestrator=orchestrator,
        execution_identity=execution_identity,
        mode=mode,
        requested_agent_names=requested_team_agent_names(
            [_member_name(member_id, registry, orchestrator.runtime_paths) for member_id in agent_ids],
        ),
        thread_history=thread_history,
        model_name=model_name,
        media=media,
        show_tool_calls=show_tool_calls,
        run_id_callback=run_id_callback,
        user_id=user_id,
        response_sender_id=response_sender_id,
        compaction_lifecycle=compaction_lifecycle,
        run_metadata_collector=run_metadata_collector,
        configured_team_name=configured_team_name,
        pipeline_timing=pipeline_timing,
        attempt_model_runtime=attempt_model_runtime,
        turn_recorder=turn_recorder,
        reason_prefix=reason_prefix,
        member_model_names=member_model_names,
    )
    if isinstance(turn, str):
        yield turn
        return

    adapter = StreamingTurnAdapter[TeamStreamChunk](
        open_scope=turn.open_scope,
        run_attempt=turn.run_streaming_attempt,
        snapshot_partial=turn.holder.partial_snapshot,
        release_attempt_entity=turn.holder.release_attempt_members,
        finalize_attempt=turn.finalize_attempt,
        make_text_chunk=lambda text: text,
        unexpected_error_text=turn.error_text,
        discard_empty_run=turn.discard_empty_run,
    )
    response_stream = stream_response_turn(
        turn.ctx,
        adapter,
        TurnSinks(turn_recorder=turn_recorder, run_metadata_collector=run_metadata_collector),
        continuation=turn.initial_continuation(
            message,
            current_timestamp_ms=current_timestamp_ms,
            current_prompt_is_structured=current_prompt_is_structured,
        ),
    )
    # Close the driver generator deterministically when this wrapper unwinds so
    # its cleanup does not wait for event-loop async-generator finalization.
    async with aclosing(response_stream) as closing_stream:
        async for chunk in closing_stream:
            yield chunk


def _member_name(agent_id: MatrixID, config: Config, runtime_paths: RuntimePaths) -> str:
    """Return the config name of the agent behind one Matrix id."""
    return (
        entity_identity_registry(config, runtime_paths).current_entity_name_for_user_id(
            agent_id.full_id,
            include_router=True,
        )
        or agent_id.username
    )


# ---------------------------------------------------------------------------
# Resuming a paused team run
# ---------------------------------------------------------------------------


def _approval_history_scope(
    member_names: tuple[str, ...],
    configured_team_name: str,
    config: Config,
    execution_identity: ToolExecutionIdentity,
    retained_scope: HistoryScope | None,
) -> HistoryScope | None:
    """Resolve retained team storage before reconstructing member executables."""
    if retained_scope is not None:
        return retained_scope
    scope_id = (
        configured_team_name
        if configured_team_name in config.teams
        else ad_hoc_team_scope_id(member_names, config.agents, requester_user_id=execution_identity.requester_id)
    )
    return HistoryScope(kind="team", scope_id=scope_id) if scope_id is not None else None


def _stored_paused_team_run(scope: ScopeSessionContext, run_id: str, session_id: str) -> TeamRunRecord:
    """Return the paused team run the approval names, from the loaded session or the store.

    A run the store holds for another session of the scope is refused, as the agent path refuses it.
    """
    persisted: AnyRunRecord | None = None
    if scope.session is not None:
        persisted = next((run for run in scope.session.runs if run.run_id == run_id), None)
    if persisted is None:
        persisted = scope.storage.get_run(run_id)
    if (
        not isinstance(persisted, TeamRunRecord)
        or persisted.session_id != session_id
        or persisted.status is not RunStatus.paused
    ):
        msg = f"Paused team run {run_id!r} is no longer available"
        raise RuntimeError(msg)
    return persisted


@dataclass(frozen=True)
class _RoutedDecisions:
    """The resume's decisions routed to the calls the paused team waits for, by ``TeamCallKey``."""

    approved: dict[TeamCallKey, ApprovedCall]
    denied: dict[TeamCallKey, str | None]
    external_results: dict[TeamCallKey, str]


def _route_decisions(
    persisted: TeamRunRecord,
    decisions: ResumeDecisions,
    *,
    toolkit_owners: Mapping[tuple[str, str], str | None],
) -> _RoutedDecisions:
    """Route each decision to the waiting member or leader call it names, checking an approved call's toolkit owner.

    An approved call is matched by the member the approval was shown for, and must still come from the toolkit the
    approval recorded, so a removed or replaced tool cannot run under an old approval. A denial or a delegation's
    result names its call by id alone; a call id two members share is refused rather than guessed at.
    """
    waiting: dict[str | None, list[RunRequirement]] = {}
    for requirement in persisted.requirements or ():
        if not requirement.is_resolved():
            waiting.setdefault(requirement.tool_execution.tool_call_id, []).append(requirement)

    def key_of(call_id: str) -> TeamCallKey:
        candidates = waiting.get(call_id, [])
        if not candidates:
            msg = "Saved approval calls no longer match the pending requirements; retry the request"
            raise RuntimeError(msg)
        if len(candidates) > 1:
            msg = f"Call {call_id!r} is pending for more than one member of the team; retry the request"
            raise RuntimeError(msg)
        return (candidates[0].member_agent_id, call_id)

    approved: dict[TeamCallKey, ApprovedCall] = {}
    for call in decisions.approved:
        candidates = [r for r in waiting.get(call.tool_call_id, []) if r.needs_confirmation]
        requirement = next((r for r in candidates if r.member_agent_id == call.invoking_agent), None) or next(
            (r for r in candidates if r.member_agent_id is None),
            None,
        )
        if requirement is None or requirement.tool_execution.tool_name != call.tool_name:
            msg = "Saved approval calls no longer match the pending requirements; retry the request"
            raise RuntimeError(msg)
        if call.toolkit_name is None or toolkit_owners.get((call.invoking_agent, call.tool_name)) != call.toolkit_name:
            msg = f"Saved approval tool {call.tool_name!r} no longer has its original toolkit; retry the request"
            raise RuntimeError(msg)
        approved[(requirement.member_agent_id, call.tool_call_id)] = call
    return _RoutedDecisions(
        approved=approved,
        denied={key_of(call_id): note for call_id, note in decisions.denied.items()},
        external_results={key_of(call_id): result for call_id, result in decisions.external_results.items()},
    )


def _resume_request(
    storage: _TeamStorage,
    persisted: TeamRunRecord,
    *,
    settings: ResolvedHistorySettings,
    plan: ResolvedReplayPlan | None,
) -> list[Message]:
    """Return the leader's resume request: its stored system message, the summary and history ``plan`` replays, then the rest."""
    history, summary = replayed_history(storage.session, storage.scope, settings, plan)
    return build_resume_request(paused=persisted.messages, summary=summary, history=history)


@dataclass(frozen=True)
class _TeamResume:
    """Continue one paused team run under a turn's hooks and storage; the ``ContinueRun`` the delegation driver calls.

    ``plan`` selects the stored history the leader's resume request replays: the turn's own plan within a turn, the
    configured policy for a saved approval.
    """

    spec: TeamSpec
    storage: _TeamStorage
    terminal: _TerminalTeamRecord
    hooks: LoopHooks
    request_template: ModelRequest
    settings: ResolvedHistorySettings
    plan: ResolvedReplayPlan | None
    user_id: str | None
    toolkit_owners: Mapping[tuple[str, str], str | None]

    def events(self, paused: TeamRunRecord, decisions: ResumeDecisions) -> AsyncIterator[TeamRunEvent]:
        """Resume ``paused`` with ``decisions`` routed to the calls the team waits for; the stream ends with ``RunFinished``.

        Routing and the loop's own coverage check run here, before the stream starts, so a resume that no longer fits
        is refused before anything runs.
        """
        routed = _route_decisions(paused, decisions, toolkit_owners=self.toolkit_owners)
        session = self.storage.session
        events = continue_team(
            self.spec,
            paused,
            _resume_request(self.storage, paused, settings=self.settings, plan=self.plan),
            approved=routed.approved,
            denied=routed.denied,
            external_results=routed.external_results,
            ctx=RunContext(
                run_id=paused.run_id,
                session_id=session.session_id,
                user_id=self.user_id,
                metadata=deepcopy(paused.metadata),
                session_state=session.session_state,
            ),
            hooks=self.hooks,
            persist=_persist_into(self.terminal, self.storage),
            request_template=self.request_template,
        )
        return _under_team_scope(events, self.storage.scope)

    async def __call__(
        self,
        paused: TeamRunRecord,
        decisions: ResumeDecisions,
        on_event: ResumeEvents | None,
    ) -> TeamRunRecord:
        """Resume ``paused`` with its delegations' results, forwarding its events, and return its terminal record.

        The driver forwards a parent's events without reading them, so member events ride along.
        """
        events = cast("AsyncIterator[RunEvent]", self.events(paused, decisions))
        return await forward_resume(events, on_event, record_type=TeamRunRecord)


@dataclass(frozen=True, slots=True)
class _MemberCall:
    """The member whose run issued one delegation call, for the driver's events about that call."""

    member_id: str
    member_name: str
    run_id: str


def _delegation_owners(record: TeamRunRecord) -> dict[str, _MemberCall]:
    """Return, by call id, the member whose paused run waits on each delegation call of a stored team pause."""
    return {
        requirement.tool_execution.tool_call_id: _MemberCall(
            requirement.member_agent_id,
            requirement.member_agent_name or requirement.member_agent_id,
            requirement.member_run_id,
        )
        for requirement in record.requirements or ()
        if requirement.needs_external_execution
        and requirement.tool_execution.tool_name in DELEGATION_TOOL_NAMES
        and requirement.tool_execution.tool_call_id is not None
        and requirement.member_agent_id is not None
        and requirement.member_run_id is not None
    }


async def _tag_member_delegation_events(
    events: AsyncIterator[TeamRunEvent],
    owners: dict[str, _MemberCall],
) -> AsyncIterator[TeamRunEvent]:
    """Route the driver's events about a member's delegation call into that member's slot.

    The driver reports the delegation call it runs as the parent's own ``ToolStarted`` and ``ToolCompleted``. In a
    team the call belongs to the member whose run paused on it, which announced the call first; ``owners`` seeds
    the calls a stored pause already holds.
    """
    async with closing_async_stream(events):
        async for event in events:
            if isinstance(event, MemberEvent) and isinstance(event.event, ToolStarted):
                tool = event.event.tool
                if tool.tool_call_id is not None and tool.tool_name in DELEGATION_TOOL_NAMES:
                    owners[tool.tool_call_id] = _MemberCall(event.member_id, event.member_name, event.run_id)
                yield event
            elif (
                isinstance(event, ToolStarted | ToolCompleted)
                and (owner := owners.get(event.tool.tool_call_id or "")) is not None
            ):
                yield MemberEvent(owner.member_id, owner.member_name, owner.run_id, event)
            else:
                yield event


def _drive_team_delegations(
    events: AsyncIterator[TeamRunEvent],
    resume: _TeamResume,
    *,
    owners: dict[str, _MemberCall],
    team_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    decisions: dict[str, bool] | None = None,
    denial_reasons: dict[str, str | None] | None = None,
    approval_calls: Sequence[ApprovalCall] = (),
) -> AsyncIterator[TeamRunEvent]:
    """Pass the run's events through; when it ends paused on a member's delegation, run the children and continue it.

    The driver sees the team run as a parent and ``resume`` continues it with the children's results. Member ids
    are their config names on the owned loop, which the driver needs as the callers of the members' delegations.
    """
    driven = drive_delegation_stream(
        cast("AsyncIterator[RunEvent]", events),
        store=resume.storage.store,
        session=resume.storage.session,
        continue_parent=resume,
        agent_name=team_name,
        run_child=run_delegated_child_response,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        refresh_scheduler=refresh_scheduler,
        decisions=decisions,
        denial_reasons=denial_reasons,
        approval_calls=approval_calls,
        member_config_names={member.spec.id: member.spec.id for member in resume.spec.members},
    )
    return _tag_member_delegation_events(cast("AsyncIterator[TeamRunEvent]", driven), owners)


async def _finished(record: TeamRunRecord) -> AsyncIterator[TeamRunEvent]:
    """Yield a stored run as the end of a stream, for the driver to continue."""
    yield RunFinished(record)


@owned_tool_execution(
    enabled=lambda *, config, runtime_paths, **_kwargs: background_tool_jobs_enabled(config, runtime_paths),
)
async def continue_paused_team_run(
    *,
    member_names: tuple[str, ...],
    mode: TeamMode,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    session_id: str,
    run_id: str,
    user_id: str,
    configured_team_name: str,
    model_name: str,
    decisions: dict[str, bool],
    denial_reasons: dict[str, str | None],
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    member_model_names: Mapping[str, str] | None = None,
    approval_calls: Sequence[ApprovalCall] = (),
    history_scope: HistoryScope | None = None,
    prior_tool_trace: Sequence[ToolTraceEntry] = (),
    prior_presentation_state: Mapping[str, object] | None = None,
    show_tool_calls: bool = True,
    tool_trace_collector: list[ToolTraceEntry] | None = None,
    progress: ProgressPublisher | None,
) -> CompletedApprovalRun | PausedAttempt:
    """Rebuild a team and continue its exact persisted paused run.

    ``progress``, when given, shows the resumed document live in the reply being continued; the terminal delivery
    stays with the caller.
    """
    with open_resolved_scope_session_context(
        agent_name=min(member_names, default=configured_team_name),
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        scope=_approval_history_scope(member_names, configured_team_name, config, execution_identity, history_scope),
    ) as scope:
        try:
            if scope is None:
                msg = "Paused team history is no longer available"
                raise RuntimeError(msg)
            set_consumption_storage(scope.storage_factory)
            persisted = _stored_paused_team_run(scope, run_id, session_id)
            delegated = has_delegation_state(persisted)
            approved_calls = tuple(call for call in approval_calls if decisions.get(call.tool_call_id))
            async with asyncio.TaskGroup() as recovery:
                required_tool_tasks = {
                    name: recovery.create_task(
                        required_approval_tool_names(
                            name,
                            tuple(call for call in approved_calls if call.invoking_agent == name),
                            config=config,
                            runtime_paths=runtime_paths,
                            execution_identity=execution_identity,
                        ),
                    )
                    for name in member_names
                }
            members = await asyncio.to_thread(
                materialize_exact_team_members,
                list(member_names),
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                refresh_scheduler=refresh_scheduler,
                supports_native_tool_approval=True,
                active_model_names=member_model_names,
                required_tool_names={name: task.result() for name, task in required_tool_tasks.items()},
            )
            built = build_team_spec(
                members=members,
                mode=mode,
                config=config,
                runtime_paths=runtime_paths,
                team_display_name=team_label(member_names),
                scope_context=scope,
                execution_identity=execution_identity,
                model_name=model_name,
                configured_team_name=configured_team_name,
            )
            toolkit_owners = tool_owners_by_agent(*members.builds)
            native = restore_native_history(built.spec.model, persisted_run=persisted, session=scope.session)
            presentation = TeamStreamPresentation.restore(
                config_names=member_names,
                show_tool_calls=show_tool_calls,
                state=prior_presentation_state,
                tool_trace=prior_tool_trace,
            )
            session = scope.session or new_scope_session(
                session_id=session_id,
                scope_id=scope.scope.scope_id,
                is_team=True,
            )
            storage = _TeamStorage(scope.storage, session, scope.scope, scope.storage_factory)
            attempt = _TeamAttempt(
                run_id=run_id,
                members=members,
                presentation=presentation,
                show_tool_calls=show_tool_calls,
                team_name=configured_team_name,
                runtime_model_name=model_name,
                prepared_history=None,
                canonical_per_member=dict.fromkeys(presentation.member_ids, ""),
            )
            settings = _team_history_settings(config, configured_team_name)
            resume = _TeamResume(
                spec=built.spec,
                storage=storage,
                terminal=attempt.terminal,
                hooks=_leader_hooks(
                    config=config,
                    runtime_paths=runtime_paths,
                    entity_name=configured_team_name,
                    scope_id=built.spec.id,
                    spec=built.spec,
                    runtime_model_name=model_name,
                    storage=storage,
                    scoped=True,
                    prepared_history=PreparedHistoryState(native_history=native),
                    prompt_id=None,
                    compaction_lifecycle=None,
                    turn_recorder=None,
                    leader_middleware=built.leader_middleware,
                ),
                request_template=ModelRequest(
                    messages=(),
                    native_compaction=native.native_compaction if native is not None else None,
                    portable_replay=native.portable_replay if native is not None else False,
                ),
                settings=settings,
                plan=configured_replay_plan(history_settings=settings, estimated_tokens=0),
                user_id=user_id,
                toolkit_owners=toolkit_owners,
            )
            # A run the delegation driver owns resumes through it with the decisions on its children's calls; any
            # other run resumes with the decisions on its own calls, and the driver continues it if it then pauses
            # on a member's delegation.
            events = _drive_team_delegations(
                _finished(persisted)
                if delegated
                else resume.events(persisted, approval_decisions(approval_calls, decisions, denial_reasons)),
                resume,
                owners=_delegation_owners(persisted),
                team_name=configured_team_name,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                refresh_scheduler=refresh_scheduler,
                decisions=decisions if delegated else None,
                denial_reasons=denial_reasons if delegated else None,
                approval_calls=approval_calls if delegated else (),
            )
            with helper_usage_context(scope):
                await _publish_team_presentation(progress, presentation)
                async with closing_async_stream(events):
                    async for event in events:
                        if attempt.apply(event):
                            await _publish_team_presentation(progress, presentation)
            continued = attempt.terminal.record
            if continued is None:
                msg = f"team run {run_id!r} ended without its terminal record"
                raise RuntimeError(msg)
            paused = paused_attempt_from_response(
                continued,
                fallback_session_id=session_id,
                fallback_run_id=run_id,
                toolkit_owners=toolkit_owners,
            )
            if paused is not None:
                return _continued_team_pause(presentation, paused)
            if continued.status is not RunStatus.completed:
                raise RuntimeError(str(continued.content or "Team continuation did not complete"))
            if tool_trace_collector is not None and show_tool_calls:
                tool_trace_collector.extend(presentation.tool_trace)
            response_text = presentation.render_body() or format_team_header(members.display_names) + _NO_TEAM_RESPONSE
            return CompletedApprovalRun(
                response_text=response_text,
                metadata_content=attempt.metadata_content(config, session_id=session_id, status=continued.status),
            )
        finally:
            try:
                await finalize_consumption()
            finally:
                _register_team_notice_storage(
                    scope_context=scope,
                    session_id=session_id,
                    entity_name=configured_team_name,
                )


async def _publish_team_presentation(
    progress: ProgressPublisher | None,
    presentation: TeamStreamPresentation,
) -> None:
    """Show the team continuation's current document in the reply it resumes."""
    if progress is not None:
        await progress(
            StructuredStreamChunk(content=presentation.render_body(), tool_trace=presentation.tool_trace),
        )


__all__ = [
    "BuiltTeam",
    "PreparedTeamExecution",
    "TeamStreamChunk",
    "build_team_spec",
    "continue_paused_team_run",
    "prepare_materialized_team_execution",
    "team_label",
    "team_response",
    "team_response_stream",
]
