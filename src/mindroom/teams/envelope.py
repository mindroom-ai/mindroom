"""Team turns: prepare a turn, run its leader, and render the reply through the turn driver.

``team_response_stream`` is the team envelope the response runner drives through the turn driver; it consumes one
``run_team`` event stream into a ``TeamStreamPresentation``, which a reply delivered whole collects. The reply shows
its trace through its chunks, keeps the configured member order, and adds each failed member run's reason when the
team run ends, as a resumed reply does. ``run_prepared_team`` runs an OpenAI-compatible API team turn under the same
leader hooks and storage.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom import ai_runtime
from mindroom.agent_loop.build import tool_owners_by_agent
from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.ai_run_metadata import build_prepared_history_metadata_content
from mindroom.constants import MATRIX_SEEN_EVENT_IDS_METADATA_KEY, is_silent_schedule_no_report_response
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import (
    ThreadHistoryRenderLimits,
    prepare_bound_team_run_context,
    render_prepared_messages_text,
)
from mindroom.history.mid_turn_compaction import without_fold_records
from mindroom.history.native import with_native_history
from mindroom.history.request_messages import build_request, replayed_history
from mindroom.history.runtime import note_prepared_history_timing
from mindroom.history.session_context import load_bound_scope_session
from mindroom.history.storage import update_scope_seen_event_ids
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
    CompletedAttempt,
    HandledAttempt,
    PausedAttempt,
    ResponseTurnContext,
    TurnAdapter,
    TurnContinuationRunState,
    TurnSinks,
    build_llm_request_log_context,
    build_matrix_run_metadata,
    pause_without_approval_error,
    paused_attempt_from_response,
    stream_response_turn,
    stream_with_llm_request_log_context,
)
from mindroom.run_storage import bind_turn_session, scope_run_storage
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.teams.attempt import TeamAttempt, team_tool_executions
from mindroom.teams.leader import LeaderRun
from mindroom.teams.members import (
    ResolvedExactTeamMembers,
    allow_direct_private_team_agents,
    build_team_spec,
    materialize_team_members,
    materialized_team_label,
    requested_team_agent_names,
    resolve_team_member_model_names,
)
from mindroom.teams.presentation import (
    NO_TEAM_RESPONSE,
    TeamStreamPresentation,
    finished_run_text,
    format_team_header,
)
from mindroom.teams.resume import TeamResume, continued_team_pause, drive_team_delegations
from mindroom.teams.runtime import leader_tool_specs
from mindroom.tool_jobs.notices import append_turn_start_job_note
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import StructuredStreamChunk

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence

    from mindroom.config.main import Config, ResolvedRuntimeModel
    from mindroom.constants import RuntimePaths
    from mindroom.history.request_messages import TurnMessages
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.history.types import CompactionLifecycle, PreparedHistoryState, ScopeSummary
    from mindroom.knowledge.utils import KnowledgeAvailabilityDetail
    from mindroom.llm.runs import TeamRunRecord
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.response_turn import EmptyRunDiscard, TurnRunState
    from mindroom.run_storage import RunStorage
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.teams.members import BuiltTeam
    from mindroom.teams.runtime import TeamRunEvent
    from mindroom.teams.spec import TeamMode
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)

_MAX_LOG_MESSAGE_LENGTH = 500  # Maximum length for messages in team response logs
_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS = ThreadHistoryRenderLimits(
    max_messages=30,
    max_message_length=200,
    missing_sender_label="Unknown",
)
type TeamStreamChunk = str | StructuredStreamChunk


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
        return render_prepared_messages_text(self.turn.in_request_order())

    def run_input(self, media: MediaInputs) -> list[Message]:
        """Return one attempt's request body, media on the prompt: the summary, the replayed history, then the turn."""
        return build_request(summary=self.summary, history=self.history, turn=self.turn, media=media)


async def prepare_materialized_team_execution(
    ctx: ResponseTurnContext,
    *,
    scope_context: ScopeSessionContext,
    built: BuiltTeam,
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
    spec = built.spec
    leader = spec.leader
    # ``build_team_spec`` renders the leader prompt, which sizes the static part of every leader request.
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
        leader_system_messages=leader.system_messages,
        leader_tools=leader_tool_specs(spec),
        model=leader.model,
        prompt=message,
        transient_context_messages=(
            (Message(role="user", content=transient_turn_context, add_to_agent_memory=False),)
            if transient_turn_context
            else ()
        ),
        thread_history=thread_history,
        runtime_paths=runtime_paths,
        config=config,
        timezone=built.timezone,
        entity_name=configured_team_name,
        history_settings=built.history_settings,
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
    # The stored team history this request replays under the resolved plan, and the scope summary.
    history, summary = replayed_history(
        scope_context.session,
        scope_context.scope,
        built.history_settings,
        prepared_history.replay_plan,
    )
    run_extra_content = build_prepared_history_metadata_content(prepared_history)
    run_metadata = build_matrix_run_metadata(
        ctx.reply_to_event_id,
        prepared_execution.unseen_event_ids,
        room_id=ctx.room_id,
        thread_id=ctx.thread_id,
        requester_id=ctx.requester_id,
        correlation_id=ctx.correlation_id,
        model_params=leader.model.model_params,
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


def _leader_run(
    built: BuiltTeam,
    prepared: PreparedTeamExecution,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    storage: RunStorage,
    configured_team_name: str | None,
    compaction_lifecycle: CompactionLifecycle | None,
    turn_recorder: TurnRecorder | None,
) -> LeaderRun:
    """Return how the leader of one prepared turn runs; a fold lands in the scope only when this request replays it."""
    history = prepared.prepared_history
    return LeaderRun(
        built=built,
        storage=storage,
        hooks=leader_loop_hooks(
            built,
            HookTurn(
                config=config,
                runtime_paths=runtime_paths,
                model_name=prepared.runtime_model_name,
                storage=storage,
                replay_plan=history.replay_plan,
                prompt_id=prepared.turn.prompt.id,
                lifecycle=compaction_lifecycle,
                turn_recorder=turn_recorder,
            ),
            entity_name=configured_team_name,
        ),
        request_template=with_native_history(ModelRequest(messages=()), history.native_history),
        model_name=prepared.runtime_model_name,
    )


def run_prepared_team(
    built: BuiltTeam,
    prepared: PreparedTeamExecution,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    scope_context: ScopeSessionContext,
    team_name: str,
    user_id: str | None,
) -> AsyncIterator[TeamRunEvent]:
    """Run one prepared turn of a configured team outside Matrix, with the leader hooks and storage a Matrix turn has.

    The run and its usage are stored in the team scope, a scope without a stored session getting its first one.
    """
    storage = scope_run_storage(scope_context)
    leader = _leader_run(
        built,
        prepared,
        config=config,
        runtime_paths=runtime_paths,
        storage=storage,
        configured_team_name=team_name,
        compaction_lifecycle=None,
        turn_recorder=None,
    )
    return leader.start(
        prepared.run_input(MediaInputs()),
        run_id=str(uuid4()),
        user_id=user_id,
        metadata=prepared.run_metadata,
        records=None,
    )


async def _persist_bound_seen_event_ids(*, scope_context: ScopeSessionContext, event_ids: list[str]) -> None:
    """Record the Matrix events this team turn consumed on the stored session row, creating the row if needed.

    The ids merge into the metadata the row holds, so nothing the run or another writer stored is put back or lost.
    The write runs in the database's lane, as the session's other writes do.
    """
    storage = scope_context.storage
    # A new session names the row to merge into, so the run's loaded session never writes its stale state back.
    record = partial(
        update_scope_seen_event_ids,
        session=scope_context.first_session(),
        scope=scope_context.scope,
        event_ids=event_ids,
    )
    await run_session_storage_operation(lambda: storage, record)


def _run_metadata_seen_event_ids(run_metadata: dict[str, Any] | None) -> list[str]:
    raw_event_ids = (run_metadata or {}).get(MATRIX_SEEN_EVENT_IDS_METADATA_KEY)
    if not isinstance(raw_event_ids, list):
        return []
    return [event_id for event_id in raw_event_ids if isinstance(event_id, str) and event_id]


# ---------------------------------------------------------------------------
# One team turn
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamTurnSpec:
    """Which team one turn runs: its members and mode, the runtime that builds them, and the models they run."""

    agent_names: Sequence[str]
    mode: TeamMode
    orchestrator: OrchestratorRuntime
    model_name: str | None = None
    # The members' models chosen for the turn; None resolves them from the room and thread.
    member_model_names: Mapping[str, str] | None = None
    configured_team_name: str | None = None
    # The Matrix account the team's reply is sent from, which its prompt names.
    response_sender_id: str | None = None
    reason_prefix: str = "Team request"


@dataclass
class _TeamTurnHolder:
    """Live per-turn team state shared between attempt closures and adapter callbacks.

    ``members`` are the members the turn's latest attempt runs with: the ones the turn built, which its first attempt
    takes (``first_members_taken``), then each later attempt's rebuild.
    """

    members: ResolvedExactTeamMembers
    first_members_taken: bool = False
    attempt_started: bool = False


@dataclass(frozen=True)
class _TeamTurn:
    """The constants of one team turn, shared by its attempts.

    ``config`` is the config the turn started with, which every attempt builds its leader and members under, as an
    agent turn runs on the config it was given; ``orchestrator`` says which agents are running now.
    """

    ctx: ResponseTurnContext
    spec: TeamTurnSpec
    config: Config
    requested_agent_names: list[str]
    run_metadata_collector: dict[str, Any] | None
    holder: _TeamTurnHolder
    # The zone of the entity whose bot runs the turn, which dates all of it.
    timezone: str

    @property
    def runtime_paths(self) -> RuntimePaths:
        return self.spec.orchestrator.runtime_paths

    @property
    def configured_team_name(self) -> str | None:
        return self.spec.configured_team_name

    @property
    def execution_identity(self) -> ToolExecutionIdentity | None:
        return self.ctx.execution_identity

    @property
    def entity_name(self) -> str:
        """The name the team's stored notices and logs carry: the configured team, or the materialized label."""
        return self.configured_team_name or self.ctx.entity_label

    @property
    def session_id(self) -> str:
        """The turn's session id; team turns always run with one."""
        assert self.ctx.session_id is not None
        return self.ctx.session_id

    def request_log_context(
        self,
        *,
        prompt: str,
        run_input: Sequence[Message],
        metadata: dict[str, object] | None,
    ) -> dict[str, object]:
        """Return the request-log context of one attempt's leader requests, which carry the team's name."""
        ctx = self.ctx
        return build_llm_request_log_context(
            RequestAttribution(
                agent_id=self.entity_name,
                session_id=self.session_id,
                room_id=ctx.room_id,
                thread_id=ctx.thread_id,
                reply_to_event_id=ctx.reply_to_event_id,
                requester_id=ctx.requester_id,
                correlation_id=ctx.correlation_id,
            ),
            prompt=prompt,
            model_prompt=None,
            full_prompt=render_prepared_messages_text(run_input),
            metadata=metadata,
        )

    async def members_for_attempt(self, continuation_state: TurnContinuationRunState) -> ResolvedExactTeamMembers:
        """Return the attempt's member set: the turn's members for its first attempt, rebuilt for every later one.

        A rebuild keeps the latest members' models unless a continuation applies a new model to the members.
        """
        holder = self.holder
        if not holder.first_members_taken:
            holder.first_members_taken = True
            return holder.members
        # TODO(simplify): an empty-reply retry or a continuation that keeps the members' models rebuilds them too.
        # Rebuild only when a continuation applies a new model to the members. Plugin authors would notice:
        # member toolkit factories and MCP servers start fewer times.
        model_names = (
            dict.fromkeys(self.requested_agent_names, continuation_state.active_model_name)
            if continuation_state.apply_model_to_team_members and continuation_state.active_model_name is not None
            else holder.members.model_names
        )
        holder.members = await asyncio.to_thread(
            materialize_team_members,
            self.requested_agent_names,
            self.spec.orchestrator,
            self.execution_identity,
            config=self.config,
            reason_prefix=self.spec.reason_prefix,
            configured_team_name=self.configured_team_name,
            active_model_names=model_names,
        )
        return holder.members

    async def start_attempt(
        self,
        run: TurnRunState,
        continuation_state: TurnContinuationRunState,
    ) -> tuple[TeamAttempt, AsyncIterator[TeamRunEvent]]:
        """Build the attempt's team and prepared input, and return its state with the run's event stream."""
        members = await self.members_for_attempt(continuation_state)
        config = self.config
        # Resolve the runtime model once here and hand it down so the team spec and the run metadata cannot disagree.
        runtime_model = config.resolve_runtime_model(
            entity_name=self.configured_team_name,
            active_model_name=continuation_state.active_model_name or self.spec.model_name,
            room_id=self.ctx.room_id,
            thread_id=self.ctx.thread_id,
            runtime_paths=self.runtime_paths,
        )
        built = build_team_spec(
            members=members,
            mode=self.spec.mode,
            config=config,
            runtime_paths=self.runtime_paths,
            scope_context=run.scope,
            execution_identity=self.execution_identity,
            model_name=runtime_model.model_name,
            configured_team_name=self.configured_team_name,
            timezone=self.timezone,
            system_enrichment=render_system_enrichment_block(self.ctx.system_enrichment_items),
        )
        prepared = await prepare_materialized_team_execution(
            self.ctx,
            scope_context=run.scope,
            built=built,
            message=continuation_state.active_prompt,
            thread_history=self.ctx.thread_history,
            config=config,
            runtime_paths=self.runtime_paths,
            runtime_model=runtime_model,
            response_sender_id=self.spec.response_sender_id,
            current_sender_id=self.ctx.current_sender_id or self.ctx.requester_id,
            current_timestamp_ms=continuation_state.active_current_timestamp_ms,
            current_event_id=continuation_state.active_current_event_id,
            current_prompt_is_structured=continuation_state.active_current_prompt_is_structured,
            compaction_lifecycle=self.ctx.compaction_lifecycle,
            configured_team_name=self.configured_team_name,
            thread_history_render_limits=_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS,
            pipeline_timing=self.ctx.pipeline_timing,
        )
        run.unseen_event_ids = prepared.unseen_event_ids
        run.run_metadata = prepared.run_metadata
        presentation = TeamStreamPresentation.new(
            members.requested_agent_names,
            members.display_names,
            show_tool_calls=self.ctx.show_tool_calls,
        )
        attempt = TeamAttempt(
            run_id=continuation_state.active_run_id or str(uuid4()),
            members=members,
            presentation=presentation,
            team_name=self.entity_name,
            runtime_model_name=prepared.runtime_model_name,
            prepared_history=prepared.prepared_history,
        )
        run.attempt = attempt
        self.holder.attempt_started = True
        logger.info("executing_team_response", agent_count=len(members.builds), mode=self.spec.mode.value)
        run_input = prepared.run_input(self.ctx.media or MediaInputs())
        logger.info(
            "team_prompt_preview",
            agents=members.display_names,
            prompt_preview=prepared.prompt_text[:_MAX_LOG_MESSAGE_LENGTH],
        )
        storage = bind_turn_session(run)
        leader = _leader_run(
            built,
            prepared,
            config=config,
            runtime_paths=self.runtime_paths,
            storage=storage,
            configured_team_name=self.configured_team_name,
            compaction_lifecycle=self.ctx.compaction_lifecycle,
            turn_recorder=run.record,
        )
        request_context = self.request_log_context(
            prompt=continuation_state.active_prompt,
            run_input=run_input,
            metadata=prepared.run_metadata,
        )
        with bind_llm_request_log_context(**request_context):
            if self.ctx.pipeline_timing is not None:
                self.ctx.pipeline_timing.mark_model_request()
            events = leader.start(
                run_input,
                run_id=attempt.run_id,
                user_id=self.ctx.requester_id,
                metadata=prepared.run_metadata,
                records=attempt.records,
            )
        events = drive_team_delegations(
            events,
            TeamResume(
                leader=leader,
                records=attempt.records,
                plan=prepared.prepared_history.replay_plan,
                user_id=self.ctx.requester_id,
                toolkit_owners=tool_owners_by_agent(*members.builds),
            ),
            team_name=self.entity_name,
            config=config,
            runtime_paths=self.runtime_paths,
            execution_identity=self.execution_identity,
            refresh_scheduler=self.spec.orchestrator.knowledge_refresh_scheduler,
        )
        return attempt, stream_with_llm_request_log_context(events, request_context=request_context)

    async def run_attempt(
        self,
        run: TurnRunState,
        continuation_state: TurnContinuationRunState,
    ) -> AsyncGenerator[TeamStreamChunk | AttemptResolved, None]:
        """Stream one team attempt as its document grows, ending with its ``AttemptResolved`` sentinel."""
        attempt, events = await self.start_attempt(run, continuation_state)
        shown: str | None = None
        try:
            async with closing_async_stream(events):
                async for event in events:
                    if not attempt.apply(event):
                        continue
                    chunk = attempt.chunk(finished=False)
                    if chunk is not None:
                        shown = chunk.content
                        yield chunk
        except Exception as error:
            logger.exception("team_response_failed", agents=attempt.members.display_names)
            yield AttemptResolved(
                HandledAttempt(
                    self.error_text(error),
                    metadata_content=self.run_metadata_content(attempt, RunStatus.error),
                    error=error,
                ),
            )
            return
        finally:
            # A failed run's deltas never show why it failed. However the team run ended, finished, failed, or stopped,
            # it stored its record before its events ended, and the document adds each failed run's reason from it, so
            # the reply and what the turn records show the reason.
            if (stored := attempt.records.record) is not None:
                attempt.show_failure_reasons(stored)
        record = attempt.terminal_record()
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
        if self.silent_no_report(record):
            # A quiet scheduled check that found nothing replies with the bare token, which delivery then suppresses,
            # instead of a team document around it.
            # The document's trace stays, as the streamed reply keeps it.
            shown = finished_run_text(record).strip()
            yield StructuredStreamChunk(content=shown, tool_trace=None)
        # The finished reply adds what no live chunk showed: a failed member run's reason, or the no-consensus note
        # when only members spoke.
        elif (chunk := attempt.chunk(finished=True)) is not None and chunk.content != shown:
            shown = chunk.content
            yield chunk
        yield AttemptResolved(await self.resolve_completed(run, attempt, record, shown=shown is not None))

    def silent_no_report(self, record: TeamRunRecord) -> bool:
        """Whether a quiet scheduled turn's leader answered with the no-report token while no member found anything."""
        return (
            self.ctx.allow_no_report_response
            and is_silent_schedule_no_report_response(finished_run_text(record))
            and not any(finished_run_text(member).strip() for member in record.ended_member_runs)
        )

    def run_metadata_content(self, attempt: TeamAttempt, status: RunStatus) -> dict[str, Any] | None:
        """Return the attempt's run metadata when the caller collects metadata and the run stored its record."""
        if self.run_metadata_collector is None or attempt.records.record is None:
            return None
        return attempt.metadata_content(
            self.config,
            runtime_paths=self.runtime_paths,
            session_id=self.session_id,
            status=status,
        )

    async def finalize_attempt(self, scope_context: ScopeSessionContext | None) -> None:
        """Register the team's storage for the turn's queued-notice finalization once an attempt ran."""
        if not self.holder.attempt_started:
            return
        self.holder.attempt_started = False
        ai_runtime.register_scope_notice_storage(scope_context=scope_context, entity_name=self.entity_name)

    def resolve_paused(self, attempt: TeamAttempt, record: TeamRunRecord) -> PausedAttempt:
        """Return the pause the lifecycle owner asks the requester about, in the document its events built.

        The pause freezes that document, which does not show the paused notices its runs hold as their content
        (``paused_content``, ``team_paused_content``); the approval card says what it waits for.
        """
        paused = paused_attempt_from_response(
            record,
            toolkit_owners=tool_owners_by_agent(*attempt.members.builds),
        )
        if paused is None:
            raise pause_without_approval_error(record, entity_label="team")
        return replace(
            continued_team_pause(attempt.presentation, paused),
            runtime_model_name=attempt.runtime_model_name,
            team_member_model_names=tuple(sorted(attempt.members.model_names.items())),
        )

    async def resolve_completed(
        self,
        run: TurnRunState,
        attempt: TeamAttempt,
        record: TeamRunRecord,
        *,
        shown: bool,
    ) -> CompletedAttempt:
        """Return the completed attempt: what its reply still has to show, what replays, and the turn's metadata.

        ``shown`` says whether the attempt's chunks showed a document; one that showed nothing ends with the
        no-response line. An attempt that produced anything, text or tool calls, records the Matrix events the turn
        consumed as seen on the stored session row; an empty one, whose run is discarded, leaves them unseen, so the
        next turn still reads them.
        """
        tool_executions = team_tool_executions(record)
        header = format_team_header(attempt.members.display_names)
        # The text records the document the attempt's events built and its failed runs' reasons, in the configured
        # member order, without tool markers: the chunks show the trace. The "No team response generated." fallback is
        # display chrome, not replayable assistant text. A quiet scheduled reply records the bare token.
        body_text = attempt.presentation.text_without_tool_markers(finished=True)
        has_visible_output = bool(body_text)
        is_empty = record.status is RunStatus.completed and not tool_executions and not has_visible_output
        if not is_empty and self.ctx.reply_to_event_id:
            await _persist_bound_seen_event_ids(
                scope_context=run.scope,
                event_ids=_run_metadata_seen_event_ids(run.run_metadata),
            )
        rendered = header + (body_text or NO_TEAM_RESPONSE)
        replayable_text = rendered if has_visible_output else ""
        if self.silent_no_report(record):
            replayable_text = finished_run_text(record).strip()
        logger.info(
            "team_response_preview",
            agents=attempt.members.display_names,
            response_preview=rendered[:_MAX_LOG_MESSAGE_LENGTH],
        )
        if len(rendered) > _MAX_LOG_MESSAGE_LENGTH:
            logger.debug("team_response_full", agents=attempt.members.display_names, response=rendered)
        return CompletedAttempt(
            response_text="" if shown else rendered,
            replayable_text=replayable_text,
            has_visible_content=has_visible_output,
            is_empty=is_empty,
            session_id=record.session_id,
            run_id=record.run_id,
            attempt_run_id=attempt.run_id,
            runtime_model_name=attempt.runtime_model_name,
            output_tokens=record.metrics.tokens.output_tokens if record.metrics is not None else None,
            tool_executions=tuple(tool_executions),
            completed_tools=tuple(attempt.presentation.tool_tracker.completed_tools),
            metadata_content=self.run_metadata_content(attempt, record.status),
        )

    def error_text(self, error: Exception) -> str:
        return get_user_friendly_error_message(error, self.ctx.entity_label, runtime_paths=self.runtime_paths)

    def discard_empty_run(self, scope_context: ScopeSessionContext | None, discard: EmptyRunDiscard) -> None:
        ai_runtime.discard_empty_completed_run(
            scope_context=scope_context,
            session_id=discard.session_id or self.session_id,
            run_id=discard.run_id,
            entity_name=self.entity_name,
            output_tokens=discard.output_tokens,
        )

    def load_scope(self) -> ScopeSessionContext:
        return load_bound_scope_session(
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


def _team_turn(spec: TeamTurnSpec, ctx: ResponseTurnContext, sinks: TurnSinks) -> _TeamTurn | str:
    """Materialize the team's members and return the turn's constants, or the text that explains why it cannot run."""
    orchestrator = spec.orchestrator
    assert orchestrator.config is not None
    if not ctx.session_id:
        msg = "team response turns require ResponseTurnContext.session_id"
        raise ValueError(msg)
    config = orchestrator.config
    requested_agent_names = requested_team_agent_names(list(spec.agent_names))
    config.assert_team_agents_supported(
        requested_agent_names,
        allow_direct_private_agents=allow_direct_private_team_agents(
            ctx.execution_identity,
            configured_team_name=spec.configured_team_name,
        ),
    )
    active_member_model_names = (
        dict(spec.member_model_names)
        if spec.member_model_names is not None
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
            ctx.execution_identity,
            config=config,
            unavailable_bases=unavailable_bases,
            reason_prefix=spec.reason_prefix,
            configured_team_name=spec.configured_team_name,
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
        spec=spec,
        config=config,
        requested_agent_names=requested_agent_names,
        run_metadata_collector=sinks.run_metadata_collector,
        holder=_TeamTurnHolder(members=team_members),
        # ``ctx`` still names the entity whose bot runs the turn; the turn's own context took the team's label above.
        timezone=config.get_entity_timezone(ctx.entity_label),
    )


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


async def team_response_stream(
    spec: TeamTurnSpec,
    ctx: ResponseTurnContext,
    *,
    sinks: TurnSinks,
) -> AsyncIterator[TeamStreamChunk]:
    """Stream one team turn as a live document: a header, one section per member, and the leader's consensus.

    The document is rebuilt as events arrive, its members in the configured order. ``ctx`` carries the turn's
    inputs (``prompt`` is the team's message); this entry refines its ``entity_label`` to the materialized team label
    and appends the knowledge-availability enrichment before the turn runs.
    """
    turn = await asyncio.to_thread(_team_turn, spec, ctx, sinks)
    if isinstance(turn, str):
        # A turn whose members cannot be built ran nothing to record; its message and this reply stay in the thread.
        yield turn
        return

    adapter = TurnAdapter[TeamStreamChunk](
        load_scope=turn.load_scope,
        run_attempt=turn.run_attempt,
        finalize_attempt=turn.finalize_attempt,
        make_text_chunk=lambda text: text,
        user_message=ctx.prompt,
        unexpected_error_text=turn.error_text,
        discard_empty_run=turn.discard_empty_run,
    )
    response_stream = stream_response_turn(
        turn.ctx,
        adapter,
        sinks,
        continuation=turn.initial_continuation(
            ctx.prompt,
            current_timestamp_ms=ctx.current_timestamp_ms,
            current_prompt_is_structured=ctx.current_prompt_is_structured,
        ),
    )
    # Close the driver generator deterministically when this wrapper unwinds so
    # its cleanup does not wait for event-loop async-generator finalization.
    async with aclosing(response_stream) as closing_stream:
        async for chunk in closing_stream:
            yield chunk


__all__ = [
    "PreparedTeamExecution",
    "TeamStreamChunk",
    "TeamTurnSpec",
    "prepare_materialized_team_execution",
    "run_prepared_team",
    "team_response_stream",
]
