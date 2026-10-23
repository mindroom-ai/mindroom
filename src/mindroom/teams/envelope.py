"""Team turns on the owned runtime: prepare a turn, run its leader, and render the reply through the turn drivers.

``team_response`` and ``team_response_stream`` are the team envelopes the response runner drives through the shared
turn drivers; both consume one ``run_team`` event stream into a ``TeamStreamPresentation``. A blocking reply delivers
no tool trace and renders from the finished run's records, as Agno's blocking team reply did: each member run's text
in the order the runs ended, then the consensus. Its pause and the text a stopped or failed blocking turn records
render from the records too. The streamed reply shows its trace through its chunks and keeps the configured order.
``run_prepared_team`` runs an OpenAI-compatible API team turn under the same leader hooks and storage.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom import ai_runtime
from mindroom.agent_loop.build import tool_owners_by_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.ai_run_metadata import build_prepared_history_metadata_content
from mindroom.constants import MATRIX_SEEN_EVENT_IDS_METADATA_KEY, is_silent_schedule_no_report_response
from mindroom.entity_resolution import entity_identity_registry
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import (
    ThreadHistoryRenderLimits,
    prepare_bound_team_run_context,
    render_prepared_team_messages_text,
)
from mindroom.history.mid_turn_compaction import without_fold_records
from mindroom.history.request_messages import build_request, replayed_history
from mindroom.history.runtime import note_prepared_history_timing
from mindroom.history.session_context import open_bound_scope_session_context
from mindroom.history.storage import new_scope_session, update_scope_seen_event_ids
from mindroom.hooks import render_enrichment_block, render_system_enrichment_block, render_transient_context
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.llm.request_context import RequestAttribution, bind_llm_request_log_context
from mindroom.llm.runs import RunStatus
from mindroom.logging_config import get_logger
from mindroom.loop_hooks import HookTurn, leader_loop_hooks
from mindroom.media_inputs import MediaInputs
from mindroom.metadata_merge import deep_merge_metadata
from mindroom.response_turn import (
    AttemptResolved,
    BlockingAttemptResolution,
    BlockingTurnAdapter,
    CompletedAttempt,
    ExcludedAttempt,
    HandledAttempt,
    PausedAttempt,
    ResponseTurnContext,
    StreamingTurnAdapter,
    TurnContinuationRunState,
    TurnPartialSnapshot,
    TurnSinks,
    build_llm_request_log_context,
    build_matrix_run_metadata,
    pause_without_approval_error,
    paused_attempt_from_response,
    run_blocking_response_turn,
    stream_response_turn,
    stream_with_llm_request_log_context,
)
from mindroom.teams.attempt import TeamAttempt, team_tool_executions
from mindroom.teams.members import (
    ResolvedExactTeamMembers,
    allow_direct_private_team_agents,
    build_team_spec,
    materialize_team_members,
    materialized_team_label,
    requested_team_agent_names,
    resolve_team_member_model_names,
    team_label,
)
from mindroom.teams.presentation import (
    NO_TEAM_RESPONSE,
    TeamStreamPresentation,
    finished_run_text,
    format_team_header,
    format_team_response,
    team_response_text,
)
from mindroom.teams.resume import TeamResume, blocking_team_pause, continued_team_pause, drive_team_delegations
from mindroom.teams.runtime import leader_tool_specs, run_team
from mindroom.teams.spec import TeamMode
from mindroom.teams.storage import (
    persist_into,
    register_team_notice_storage,
    scope_storage,
    team_history_settings,
    under_team_scope,
)
from mindroom.tool_jobs.notices import append_turn_start_job_note
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import StructuredStreamChunk

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.config.main import Config, ResolvedRuntimeModel
    from mindroom.constants import RuntimePaths
    from mindroom.history.request_messages import TurnMessages
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.history.types import CompactionLifecycle, PreparedHistoryState, ScopeSummary
    from mindroom.knowledge.utils import KnowledgeAvailabilityDetail
    from mindroom.llm.runs import TeamRunRecord
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.response_turn import EmptyRunDiscard, TurnRunState
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.teams.members import BuiltTeam
    from mindroom.teams.runtime import TeamRunEvent
    from mindroom.teams.spec import TeamSpec
    from mindroom.teams.storage import TeamRunRecords, TeamStorage
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)

# Message length limits for team context and logging
_MAX_CONTEXT_MESSAGE_LENGTH = 200  # Maximum length for messages to include in thread context
_MAX_LOG_MESSAGE_LENGTH = 500  # Maximum length for messages in team response logs
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
        RequestAttribution(
            agent_id=team_name,
            session_id=ctx.session_id or "",
            room_id=ctx.room_id,
            thread_id=ctx.thread_id,
            reply_to_event_id=ctx.reply_to_event_id,
            requester_id=ctx.requester_id,
            correlation_id=ctx.correlation_id,
        ),
        prompt=prompt,
        model_prompt=None,
        full_prompt=_team_run_input_text(run_input),
        metadata=metadata,
    )


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
        team_history_settings(config, configured_team_name),
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
        model_params=spec.model.model_params,
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
# Running the leader and storing the turn
# ---------------------------------------------------------------------------


def _team_storage(run: TurnRunState, *, session_id: str) -> TeamStorage | None:
    """Return where one attempt's team run is stored; the turn's scope context keeps the session a run first gave it.

    A team without a bound scope stores nothing, as Agno's team ran without a database then.
    """
    scope_context = run.scope_context
    if scope_context is None:
        return None
    storage = scope_storage(scope_context, session_id=session_id)
    if scope_context.session is None:
        run.scope_context = replace(scope_context, session=storage.session)
    return storage


@dataclass(frozen=True)
class _LeaderRun:
    """How the leader of one prepared team turn runs: where it stores, the hooks around its loop, its request settings."""

    spec: TeamSpec
    storage: TeamStorage | None
    hooks: LoopHooks
    request_template: ModelRequest

    def start(
        self,
        run_input: Sequence[Message],
        *,
        run_id: str,
        session_id: str,
        user_id: str | None,
        metadata: dict[str, Any] | None,
        records: TeamRunRecords | None,
    ) -> AsyncIterator[TeamRunEvent]:
        """Start the run; its tools share the stored session's ``session_state``, which the store writes back."""
        return run_team(
            self.spec,
            run_input,
            ctx=RunContext(
                run_id=run_id,
                session_id=session_id,
                user_id=user_id,
                metadata=metadata,
                session_state=self.storage.session.session_state if self.storage is not None else {},
            ),
            hooks=self.hooks,
            persist=persist_into(records, self.storage),
            request_template=self.request_template,
        )


def _leader_run(
    built: BuiltTeam,
    prepared: PreparedTeamExecution,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    storage: TeamStorage | None,
    configured_team_name: str | None,
    stream: bool,
    compaction_lifecycle: CompactionLifecycle | None,
    turn_recorder: TurnRecorder | None,
) -> _LeaderRun:
    """Return how the leader of one prepared turn runs; a fold lands in the scope only when this request replays it."""
    history = prepared.prepared_history
    native = history.native_history
    return _LeaderRun(
        spec=built.spec,
        storage=storage,
        hooks=leader_loop_hooks(
            HookTurn(
                config=config,
                runtime_paths=runtime_paths,
                model_name=prepared.runtime_model_name,
                store=storage.store if storage is not None else None,
                session=storage.session if storage is not None else None,
                scope=storage.scope if storage is not None else None,
                replay_plan=history.replay_plan,
                prompt_id=prepared.turn.prompt.id,
                lifecycle=compaction_lifecycle,
                turn_recorder=turn_recorder,
            ),
            entity_name=configured_team_name,
            scope_id=built.spec.id,
            model=built.spec.model,
            history_settings=team_history_settings(config, configured_team_name),
            tool_middleware=built.leader_middleware,
            jobs=built.leader_jobs,
        ),
        request_template=ModelRequest(
            messages=(),
            stream=stream,
            native_compaction=native.native_compaction if native is not None else None,
            portable_replay=native.portable_replay if native is not None else False,
        ),
    )


def run_prepared_team(
    built: BuiltTeam,
    prepared: PreparedTeamExecution,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    scope_context: ScopeSessionContext | None,
    team_name: str,
    session_id: str,
    user_id: str | None,
    stream: bool,
) -> AsyncIterator[TeamRunEvent]:
    """Run one prepared turn of a configured team outside Matrix, with the leader hooks and storage a Matrix turn has.

    The run and its usage are stored in the team scope, a scope without a stored session getting its first one.
    """
    storage = scope_storage(scope_context, session_id=session_id) if scope_context is not None else None
    leader = _leader_run(
        built,
        prepared,
        config=config,
        runtime_paths=runtime_paths,
        storage=storage,
        configured_team_name=team_name,
        stream=stream,
        compaction_lifecycle=None,
        turn_recorder=None,
    )
    return leader.start(
        prepared.run_input(MediaInputs()),
        run_id=str(uuid4()),
        session_id=session_id,
        user_id=user_id,
        metadata=prepared.run_metadata,
        records=None,
    )


def _persist_bound_seen_event_ids(
    *,
    scope_context: ScopeSessionContext | None,
    session_id: str,
    event_ids: list[str],
) -> None:
    """Record the Matrix events this team turn consumed on the stored session row, creating the row if needed.

    The ids merge into the metadata the row holds, so nothing the run or another writer stored is put back or lost.
    """
    if scope_context is None:
        return
    session = new_scope_session(session_id=session_id, scope_id=scope_context.scope.scope_id, is_team=True)
    update_scope_seen_event_ids(scope_context.storage, session, scope_context.scope, event_ids)


def _run_metadata_seen_event_ids(run_metadata: dict[str, Any] | None) -> list[str]:
    if not isinstance(run_metadata, dict):
        return []
    raw_event_ids = run_metadata.get(MATRIX_SEEN_EVENT_IDS_METADATA_KEY)
    if not isinstance(raw_event_ids, list):
        return []
    return [event_id for event_id in raw_event_ids if isinstance(event_id, str) and event_id]


# ---------------------------------------------------------------------------
# One team turn
# ---------------------------------------------------------------------------


@dataclass
class _TeamTurnHolder:
    """Live per-turn team state shared between attempt closures and adapter callbacks."""

    team_members: ResolvedExactTeamMembers | None = None
    member_model_names: dict[str, str] = field(default_factory=dict)
    attempt: TeamAttempt | None = None
    attempt_started: bool = False

    def partial_snapshot(self) -> TurnPartialSnapshot:
        """Return the live partial-output view, empty between a released attempt and the next."""
        return self.attempt.partial_snapshot() if self.attempt is not None else TurnPartialSnapshot()

    def release_attempt_members(self, _scope_context: ScopeSessionContext | None) -> None:
        """Drop the spent attempt's members so the next attempt rebuilds them with the switched model.

        Cancel snapshots taken between this release and the next attempt must not see the spent attempt's partials;
        its completed tools already carried over via the turn state.
        """
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
    ) -> tuple[TeamAttempt, AsyncIterator[TeamRunEvent]]:
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
        attempt = TeamAttempt(
            run_id=continuation_state.active_run_id or str(uuid4()),
            members=members,
            presentation=presentation,
            show_tool_calls=self.show_tool_calls,
            stream=stream,
            team_name=self.entity_name,
            runtime_model_name=prepared.runtime_model_name,
            prepared_history=prepared.prepared_history,
        )
        self.holder.attempt = attempt
        self.holder.attempt_started = True
        logger.info("executing_team_response", agent_count=len(members.builds), mode=self.mode.value)
        run_input = prepared.run_input(self.media)
        logger.info("team_prompt_preview", agents=members.display_names, prompt_preview=prepared.prompt_text[:500])
        storage = _team_storage(run, session_id=self.session_id)
        leader = _leader_run(
            built,
            prepared,
            config=config,
            runtime_paths=self.runtime_paths,
            storage=storage,
            configured_team_name=self.configured_team_name,
            stream=stream,
            compaction_lifecycle=self.compaction_lifecycle,
            turn_recorder=self.turn_recorder,
        )
        ai_runtime.note_attempt_run_id(self.run_id_callback, attempt.run_id)
        request_context = _team_request_log_context(
            self.ctx,
            team_name=self.entity_name,
            prompt=continuation_state.active_prompt,
            run_input=run_input,
            metadata=prepared.run_metadata,
        )
        with bind_llm_request_log_context(**request_context):
            if self.pipeline_timing is not None:
                self.pipeline_timing.mark_model_request()
            events = leader.start(
                run_input,
                run_id=attempt.run_id,
                session_id=self.session_id,
                user_id=self.user_id,
                metadata=prepared.run_metadata,
                records=attempt.records,
            )
        events = under_team_scope(events, storage.scope if storage is not None else None)
        if storage is not None:
            events = drive_team_delegations(
                events,
                TeamResume(
                    spec=built.spec,
                    storage=storage,
                    records=attempt.records,
                    hooks=leader.hooks,
                    # A delegation continuation streams its requests even in a blocking turn, as Agno's did.
                    request_template=replace(leader.request_template, stream=True),
                    settings=team_history_settings(config, self.configured_team_name),
                    plan=prepared.prepared_history.replay_plan,
                    user_id=self.user_id,
                    toolkit_owners=tool_owners_by_agent(*members.builds),
                ),
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
                partial_text=attempt.interrupted_text(),
                completed_tools=tuple(attempt.tool_tracker.completed_tools),
                interrupted_tools=tuple(pending.trace_entry for pending in attempt.tool_tracker.pending_tools),
                session_id=self.session_id,
                run_id=attempt.run_id,
                metadata_content=self.failure_metadata(attempt),
            )
        finally:
            # The run stores its record before its events end, however they end, so a stop from here until the turn
            # settles records the run's records.
            self.sync_live_turn_recorder(run, attempt)
        record = attempt.records.terminal(self.entity_name)
        if record.status is RunStatus.paused:
            return self.resolve_paused(attempt, record, blocking=True)
        return self.resolve_completed(run, attempt, record, blocking=True, mark_seen=True)

    async def run_streaming_attempt(
        self,
        run: TurnRunState,
        continuation_state: TurnContinuationRunState,
    ) -> AsyncGenerator[TeamStreamChunk | AttemptResolved, None]:
        """Stream one team attempt as its document grows, ending with its ``AttemptResolved`` sentinel."""
        attempt, events = await self.start_attempt(run, continuation_state, stream=True)
        emitted_output = False
        try:
            async with closing_async_stream(events):
                async for event in events:
                    if not attempt.apply(event):
                        continue
                    self.sync_live_turn_recorder(run, attempt)
                    chunk = attempt.chunk()
                    if chunk is not None:
                        emitted_output = True
                        yield chunk
        except Exception as error:
            logger.exception("team_streaming_failed", agents=attempt.members.display_names)
            self.sync_live_turn_recorder(run, attempt)
            yield self.error_text(error)
            yield AttemptResolved(HandledAttempt(metadata_content=self.failure_metadata(attempt)))
            return
        record = attempt.records.terminal(self.entity_name)
        if record.status is RunStatus.paused:
            paused = self.resolve_paused(attempt, record, blocking=False)
            if paused.response_text:
                yield StructuredStreamChunk(
                    content=paused.response_text,
                    tool_trace=list(paused.tool_trace),
                    presentation_state=paused.response_presentation_state,
                )
            yield AttemptResolved(paused)
            return
        # An attempt that streamed nothing writes no seen ids: when every attempt comes back empty, no stored run
        # carries the trigger either, so the next turn still reads it.
        yield AttemptResolved(self.resolve_completed(run, attempt, record, blocking=False, mark_seen=emitted_output))

    def failure_metadata(self, attempt: TeamAttempt) -> dict[str, Any] | None:
        """Return the run metadata of a failed attempt when the caller collects metadata and the run was stored."""
        if self.run_metadata_collector is None or attempt.records.record is None:
            return None
        return attempt.metadata_content(self.config, session_id=self.session_id, status=RunStatus.error)

    async def finalize_attempt(self, scope_context: ScopeSessionContext | None) -> None:
        """Register the team's storage for the turn's queued-notice finalization once an attempt ran."""
        if not self.holder.attempt_started:
            return
        self.holder.attempt_started = False
        self.register_notice_storage(scope_context)

    def sync_live_turn_recorder(self, run: TurnRunState, attempt: TeamAttempt) -> None:
        """Refresh the live turn state from the attempt, so an interruption records its text and its tool calls."""
        run.turn_state.sync_partial(
            self.turn_recorder,
            run_metadata=run.run_metadata,
            assistant_text=attempt.interrupted_text(),
            completed_tools=attempt.tool_tracker.completed_tools,
            interrupted_tools=[pending.trace_entry for pending in attempt.tool_tracker.pending_tools],
        )

    def resolve_paused(self, attempt: TeamAttempt, record: TeamRunRecord, *, blocking: bool) -> PausedAttempt:
        """Return the pause the lifecycle owner asks the requester about, rendered into the document it shows.

        A blocking pause renders its runs' records, as Agno's blocking team pause did; a streamed one freezes the
        document its events built.
        """
        paused = paused_attempt_from_response(
            record,
            fallback_session_id=self.session_id,
            fallback_run_id=attempt.run_id,
            toolkit_owners=tool_owners_by_agent(*attempt.members.builds),
        )
        if paused is None:
            raise pause_without_approval_error(record, entity_label="team")
        presented = (
            blocking_team_pause(
                paused,
                record,
                record.ended_member_runs,
                config_names=attempt.members.requested_agent_names,
                display_names=attempt.members.display_names,
                show_tool_calls=attempt.show_tool_calls,
            )
            if blocking
            else continued_team_pause(attempt.presentation, paused)
        )
        return replace(
            presented,
            runtime_model_name=attempt.runtime_model_name,
            team_member_model_names=tuple(sorted(self.holder.member_model_names.items())),
        )

    def resolve_completed(
        self,
        run: TurnRunState,
        attempt: TeamAttempt,
        record: TeamRunRecord,
        *,
        blocking: bool,
        mark_seen: bool,
    ) -> CompletedAttempt:
        """Return the completed attempt: its rendered body, what replays, and the turn's metadata.

        ``mark_seen`` records the Matrix events the turn consumed as seen on the stored session row.
        """
        if mark_seen and self.ctx.reply_to_event_id:
            _persist_bound_seen_event_ids(
                scope_context=run.scope_context,
                session_id=self.session_id,
                event_ids=_run_metadata_seen_event_ids(run.run_metadata),
            )
        members = record.ended_member_runs
        tool_executions = team_tool_executions(record, members)
        header = format_team_header(attempt.members.display_names)
        # Neither path's text carries tool markers: a blocking reply delivers no tool trace, and a streamed reply shows
        # its trace through its chunks. A blocking reply renders the finished runs, as Agno's did; a streamed one
        # records the document its events built, in the configured member order. The "No team response generated."
        # fallback is display chrome, not replayable assistant text.
        if blocking:
            leader_text = finished_run_text(record)
            has_visible_output = bool(format_team_response(record, members)) or bool(leader_text.strip())
            rendered = header + team_response_text(record, members)
            silent = (
                self.ctx.allow_no_report_response
                and is_silent_schedule_no_report_response(leader_text)
                and not any(finished_run_text(member).strip() for member in members)
            )
            # A quiet scheduled reply delivers and records the bare token, not the team document.
            response_text = leader_text.strip() if silent else rendered
            replayable_text = response_text if has_visible_output else ""
        else:
            body_text = attempt.reply.canonical_text()
            has_visible_output = bool(body_text)
            rendered = header + (body_text or NO_TEAM_RESPONSE)
            response_text = ""
            replayable_text = rendered if has_visible_output else ""
        logger.info(
            "team_response_preview",
            agents=attempt.members.display_names,
            response_preview=rendered[:_MAX_LOG_MESSAGE_LENGTH],
        )
        if len(rendered) > _MAX_LOG_MESSAGE_LENGTH:
            logger.debug("team_response_full", agents=attempt.members.display_names, response=rendered)
        metadata_content = (
            attempt.metadata_content(self.config, session_id=self.session_id, status=record.status)
            if self.run_metadata_collector is not None
            else None
        )
        return CompletedAttempt(
            response_text=response_text,
            replayable_text=replayable_text,
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
        register_team_notice_storage(
            scope_context=scope_context,
            session_id=self.session_id,
            entity_name=self.entity_name,
        )

    def discard_empty_run(self, scope_context: ScopeSessionContext | None, discard: EmptyRunDiscard) -> None:
        ai_runtime.discard_empty_completed_run(
            scope_context=scope_context,
            session_id=discard.session_id or self.session_id,
            run_id=discard.run_id,
            entity_name=self.entity_name,
            output_tokens=discard.output_tokens,
        )

    def open_scope(self) -> AbstractContextManager[ScopeSessionContext | None]:
        return open_bound_scope_session_context(
            member_names=self.requested_agent_names,
            session_id=self.session_id,
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
            entity_label=materialized_team_label(team_members.display_names),
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

    The document is rebuilt as events arrive, its members in the configured order. ``ctx`` carries the per-turn
    Matrix identity; this entry refines its ``entity_label`` to the materialized team label and appends the
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


__all__ = [
    "PreparedTeamExecution",
    "TeamStreamChunk",
    "prepare_materialized_team_execution",
    "run_prepared_team",
    "team_response",
    "team_response_stream",
]
