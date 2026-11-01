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

from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.ai_run_metadata import build_prepared_history_metadata_content, with_prior_usage
from mindroom.config.resolution import entity_timezone
from mindroom.constants import is_silent_schedule_no_report_response
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import (
    ThreadHistoryRenderLimits,
    prepare_bound_team_run_context,
    render_prepared_messages_text,
)
from mindroom.history.interrupted_replay import TurnInput
from mindroom.history.mid_turn_compaction import without_run_usage
from mindroom.history.native import with_native_history
from mindroom.history.request_messages import build_request, replayed_history
from mindroom.history.runtime import note_prepared_history_timing
from mindroom.history.session_context import load_bound_scope_session
from mindroom.hooks import render_enrichment_block, render_system_enrichment_block, render_transient_context
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.llm.request_context import RequestAttribution, bind_llm_request_log_context
from mindroom.llm.runs import RunStatus, run_tool_executions
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
    is_empty_reply,
    pause_without_approval_error,
    paused_attempt_from_response,
    replays_as_turn,
    stream_response_turn,
    stream_with_llm_request_log_context,
)
from mindroom.run_storage import bind_turn_session, scope_run_storage
from mindroom.teams.attempt import TeamAttempt
from mindroom.teams.leader import LeaderRun
from mindroom.teams.members import (
    ResolvedExactTeamMembers,
    build_team_spec,
    materialized_team_label,
    resolve_team_members,
)
from mindroom.teams.presentation import (
    NO_TEAM_RESPONSE,
    TeamStreamPresentation,
    format_team_header,
)
from mindroom.teams.resume import TeamResume, continued_team_pause, drive_team_delegations
from mindroom.teams.runtime import leader_tool_specs, team_usage
from mindroom.tool_jobs.notices import append_turn_start_job_note
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import FailedReply, StructuredStreamChunk
from mindroom.turn_model import ModelScope, TurnModel, resolve_turn_model

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.history.request_messages import TurnMessages
    from mindroom.history.session_context import ScopeSessionContext
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.history.types import CompactionLifecycle, PreparedHistoryState, ScopeSummary
    from mindroom.knowledge.utils import KnowledgeAvailabilityDetail
    from mindroom.llm.runs import AnyRunRecord, TeamRunRecord
    from mindroom.llm.usage import RunMetrics
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.response_turn import TurnRunState
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
type TeamStreamChunk = str | StructuredStreamChunk | FailedReply


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
    runtime_model: TurnModel,
    response_sender_id: str | None,
    current_sender_id: str | None,
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
        extra_metadata=deep_merge_metadata(without_run_usage(ctx.matrix_run_metadata), run_extra_content),
    )
    return PreparedTeamExecution(
        turn=prepared_execution.turn,
        history=tuple(history),
        summary=summary,
        run_metadata=run_metadata,
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
                prepared_context_tokens=history.prepared_context_tokens,
            ),
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


# ---------------------------------------------------------------------------
# One team turn
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamTurnSpec:
    """Which team one turn runs: its members and mode, the runtime that builds them, and the models they run."""

    agent_names: Sequence[str]
    mode: TeamMode
    orchestrator: OrchestratorRuntime
    # The members' models chosen for the turn.
    member_model_names: Mapping[str, str]
    model_name: str | None = None
    configured_team_name: str | None = None
    # The Matrix account the team's reply is sent from, which its prompt names.
    response_sender_id: str | None = None


@dataclass
class _TeamTurn:
    """The constants of one team turn, shared by its attempts, and the members its latest attempt runs with.

    ``members`` are the ones the turn resolved, on the model the latest switch for the current response chose.
    ``config`` is the config the turn started with, which every attempt builds its leader and members under, as an
    agent turn runs on the config it was given; ``spec.orchestrator`` supplies the runtime paths and the knowledge
    refresh scheduler.
    """

    ctx: ResponseTurnContext
    spec: TeamTurnSpec
    config: Config
    members: ResolvedExactTeamMembers
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
        """The name the team's run records and request logs carry: the configured team, or the materialized label."""
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

    def members_for_attempt(self, continuation_state: TurnContinuationRunState) -> ResolvedExactTeamMembers:
        """Return the attempt's members: the latest attempt's, on the model a switch moved the turn to.

        Each attempt's team builds only the members its leader delegates to.
        """
        if continuation_state.active_model_name is not None:
            self.members = replace(
                self.members,
                model_names=dict.fromkeys(self.members.requested_agent_names, continuation_state.active_model_name),
            )
        return self.members

    async def start_attempt(
        self,
        run: TurnRunState,
        continuation_state: TurnContinuationRunState,
    ) -> tuple[TeamAttempt, AsyncIterator[TeamRunEvent]]:
        """Build the attempt's team and prepared input, and return its state with the run's event stream."""
        members = self.members_for_attempt(continuation_state)
        config = self.config
        # Resolve the runtime model once here and hand it down so the team spec and the run metadata cannot disagree.
        runtime_model = resolve_turn_model(
            config,
            self.runtime_paths,
            ModelScope(
                entity=self.configured_team_name,
                room_id=self.ctx.room_id,
                thread_id=self.ctx.thread_id,
                explicit=continuation_state.active_model_name or self.spec.model_name,
            ),
            None,
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
            thread_history_render_limits=_MATRIX_TEAM_THREAD_HISTORY_RENDER_LIMITS,
            pipeline_timing=self.ctx.pipeline_timing,
        )
        run.run_metadata = prepared.run_metadata
        run.turn_input = TurnInput(context=prepared.turn.context)
        presentation = TeamStreamPresentation.new(
            members.requested_agent_names,
            members.display_names,
            show_tool_calls=self.ctx.show_tool_calls,
        )
        attempt = TeamAttempt(
            run_id=continuation_state.active_run_id or str(uuid4()),
            members=members,
            team=built.spec,
            presentation=presentation,
            team_name=self.entity_name,
            runtime_model_name=prepared.runtime_model_name,
            prepared_history=prepared.prepared_history,
        )
        run.attempt = attempt
        logger.info(
            "executing_team_response",
            agent_count=len(members.requested_agent_names),
            mode=self.spec.mode.value,
        )
        run_input = prepared.run_input(self.ctx.media or MediaInputs())
        logger.info(
            "team_prompt_preview",
            agents=members.display_names,
            prompt_preview=prepared.prompt_text[:_MAX_LOG_MESSAGE_LENGTH],
        )
        storage = bind_turn_session(run, replays=self.replays)
        leader = _leader_run(
            built,
            prepared,
            config=config,
            runtime_paths=self.runtime_paths,
            storage=storage,
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
                # The team run's record keeps the usage of the attempts the turn spent before it.
                metadata=with_prior_usage(prepared.run_metadata, run.prior_usage),
                records=attempt.records,
            )
        events = drive_team_delegations(
            events,
            TeamResume(
                leader=leader,
                records=attempt.records,
                plan=prepared.prepared_history.replay_plan,
                user_id=self.ctx.requester_id,
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
    ) -> AsyncGenerator[TeamStreamChunk | AttemptResolved]:
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
                    FailedReply(self.error_text(error), error),
                    build_run_metadata=self.run_metadata_builder(attempt, RunStatus.error, run.prior_usage),
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
            shown = (record.handover_content or "").strip()
            yield StructuredStreamChunk(content=shown, tool_trace=None)
        # The finished reply adds what no live chunk showed: a failed member run's reason, or the no-consensus note
        # when only members spoke.
        elif (chunk := attempt.chunk(finished=True)) is not None and chunk.content != shown:
            shown = chunk.content
            yield chunk
        yield AttemptResolved(self.resolve_completed(attempt, record, run.prior_usage, shown=shown is not None))

    def silent_no_report(self, record: TeamRunRecord) -> bool:
        """Whether a quiet scheduled turn's leader answered with the no-report token while no member found anything."""
        return (
            self.ctx.allow_no_report_response
            and is_silent_schedule_no_report_response(record.handover_content or "")
            and not any((member.handover_content or "").strip() for member in record.ended_member_runs)
        )

    def run_metadata_builder(
        self,
        attempt: TeamAttempt,
        status: RunStatus,
        prior_usage: tuple[RunMetrics, ...],
    ) -> Callable[[], dict[str, Any]] | None:
        """Return how to build the attempt's run metadata, or None when the run stored no record.

        ``prior_usage`` is the usage of the turn's attempts before this one, which it publishes if it ends the turn.
        """
        if attempt.records.record is None:
            return None
        return partial(
            attempt.metadata_content,
            self.config,
            session_id=self.session_id,
            status=status,
            prior_usage=prior_usage,
        )

    def resolve_paused(self, attempt: TeamAttempt, record: TeamRunRecord) -> PausedAttempt:
        """Return the pause the lifecycle owner asks the requester about, in the document its events built.

        The pause freezes that document; the approval card says what it waits for.
        """
        paused = paused_attempt_from_response(record, toolkit_owners=attempt.team.tool_owners())
        if paused is None:
            raise pause_without_approval_error(record, entity_label="team")
        return replace(
            continued_team_pause(attempt.presentation, paused),
            runtime_model_name=attempt.runtime_model_name,
            team_member_model_names=tuple(sorted(attempt.members.model_names.items())),
        )

    def resolve_completed(
        self,
        attempt: TeamAttempt,
        record: TeamRunRecord,
        prior_usage: tuple[RunMetrics, ...],
        *,
        shown: bool,
    ) -> CompletedAttempt:
        """Return the completed attempt: what its reply still has to show, what replays, and the turn's metadata.

        ``shown`` says whether the attempt's chunks showed a document; one that showed nothing ends with the
        no-response line.
        """
        tool_executions = run_tool_executions(record)
        header = format_team_header(attempt.members.display_names)
        # The text records the document the attempt's events built and its failed runs' reasons, in the configured
        # member order, without tool markers: the chunks show the trace. The "No team response generated." fallback is
        # display chrome, not replayable assistant text. A quiet scheduled reply records the bare token.
        body_text = attempt.presentation.text_without_tool_markers(finished=True)
        has_visible_output = bool(body_text)
        is_empty = is_empty_reply(record)
        rendered = header + (body_text or NO_TEAM_RESPONSE)
        replayable_text = rendered if has_visible_output else ""
        if self.silent_no_report(record):
            replayable_text = (record.handover_content or "").strip()
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
            run_id=record.run_id,
            attempt_run_id=attempt.run_id,
            usage=team_usage(record),
            tool_executions=tuple(tool_executions),
            completed_tools=tuple(attempt.presentation.tool_tracker.completed_tools),
            build_run_metadata=self.run_metadata_builder(attempt, record.status, prior_usage),
        )

    def replays(self, record: AnyRunRecord) -> bool:
        """Whether the turn stores ``record`` as a turn (``replays_as_turn``), reading an empty reply from it alone.

        A team run's events reach the attempt through a queue, so its document can lag the record's hand-over; the
        record's content holds every text the leader finished with, and a leader that called no tool ran no member.
        """
        return replays_as_turn(self.ctx, record, is_empty=is_empty_reply(record))

    def error_text(self, error: Exception) -> str:
        return get_user_friendly_error_message(error, self.ctx.entity_label, runtime_paths=self.runtime_paths)

    def load_scope(self) -> ScopeSessionContext:
        return load_bound_scope_session(
            member_names=self.members.requested_agent_names,
            session_id=self.session_id,
            runtime_paths=self.runtime_paths,
            config=self.config,
            execution_identity=self.execution_identity,
            team_name=self.configured_team_name,
        )

    def initial_continuation(self) -> TurnContinuationRunState:
        """Return the turn's first continuation state, which answers the team's message."""
        return TurnContinuationRunState.initial(
            prompt=self.ctx.prompt,
            model_prompt=None,
            current_timestamp_ms=self.ctx.current_timestamp_ms,
            current_prompt_is_structured=self.ctx.current_prompt_is_structured,
            current_event_id=self.ctx.reply_to_event_id,
            run_id=self.ctx.run_id,
            continuation_model_prompt_tail="",
        )


def _team_turn(spec: TeamTurnSpec, ctx: ResponseTurnContext) -> _TeamTurn | FailedReply:
    """Resolve the team's members and return the turn's constants, or the reply that explains why it cannot run.

    Resolving the members' knowledge reads the filesystem, so the caller runs this off the event loop
    (#1260); the members themselves build on their first delegation.
    """
    orchestrator = spec.orchestrator
    assert orchestrator.config is not None
    if not ctx.session_id:
        msg = "team response turns require ResponseTurnContext.session_id"
        raise ValueError(msg)
    config = orchestrator.config
    unavailable_bases: dict[str, KnowledgeAvailabilityDetail] = {}
    try:
        team_members = resolve_team_members(
            spec.agent_names,
            orchestrator,
            ctx.execution_identity,
            config=config,
            unavailable_bases=unavailable_bases,
            reason_prefix="Team request",
            configured_team_name=spec.configured_team_name,
            active_model_names=spec.member_model_names,
        )
    except ValueError as exc:
        return FailedReply(str(exc), exc)
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
        members=team_members,
        # ``ctx`` still names the entity whose bot runs the turn; the turn's own context took the team's label above.
        timezone=entity_timezone(config, ctx.entity_label),
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
    turn = await asyncio.to_thread(_team_turn, spec, ctx)
    if isinstance(turn, FailedReply):
        # A turn whose members cannot run ran nothing to record; its message and this reply stay in the thread.
        yield turn
        return

    adapter = TurnAdapter[TeamStreamChunk](
        load_scope=turn.load_scope,
        run_attempt=turn.run_attempt,
        make_text_chunk=lambda text: text,
        user_message=ctx.prompt,
        unexpected_error_text=turn.error_text,
    )
    response_stream = stream_response_turn(
        turn.ctx,
        adapter,
        sinks,
        continuation=turn.initial_continuation(),
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
