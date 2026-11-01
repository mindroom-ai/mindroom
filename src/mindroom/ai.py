"""AI integration module for MindRoom agents and memory management."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from functools import cached_property, partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.agent_cli.cli_shell_agent import bind_standard_cli
from mindroom.agent_cli.events import stream_cli_events
from mindroom.agent_cli.lifetime import cli_control_calls
from mindroom.agent_cli.minimal_agent import minimal_attempt, minimal_turn, presented_build
from mindroom.agent_loop.build import prompt_for_turn, tool_owners_by_agent
from mindroom.agent_loop.run import run_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.agents import agent_build_can_overlap_file_memory, create_agent
from mindroom.ai_run_metadata import (
    build_ai_run_metadata_content,
    build_prepared_history_metadata_content,
    with_prior_usage,
)
from mindroom.approval.resume import resume_from_session
from mindroom.budgets.pricing import model_prices
from mindroom.config.resolution import memory_backend as agent_memory_backend
from mindroom.delegation.execution import drive_delegation_stream
from mindroom.delegation.lifecycle import (
    authorize_delegation,
    child_tool_runtime_context,
    delegation_grant,
    note_child_run_id,
    observe_child_event,
)
from mindroom.delegation.state import child_execution_identity
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import prepare_agent_execution_context, render_prepared_messages_text
from mindroom.history.interrupted_replay import TurnInput, stored_turn_input
from mindroom.history.mid_turn_compaction import without_run_usage
from mindroom.history.native import with_native_history
from mindroom.history.replay import compaction_marker
from mindroom.history.request_messages import build_request, replayed_history
from mindroom.history.runtime import note_prepared_history_timing
from mindroom.history.session_context import ScopeSessionContext, load_scope_session
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    render_enrichment_block,
    render_system_enrichment_block,
    render_transient_context,
)
from mindroom.knowledge.utils import resolve_agent_knowledge_access_async
from mindroom.llm.events import ReasoningDelta, RunFinished, TextDelta, ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.request_context import RequestAttribution, bind_llm_request_log_context
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.logging_config import get_logger
from mindroom.loop_hooks import HookTurn, agent_loop_hooks
from mindroom.media_inputs import MediaInputs
from mindroom.memory import (
    ConversationRef,
    MemoryPromptParts,
    build_memory_prompt_parts,
    conversation_entrypoint_preamble,
    strip_user_turn_time_prefix,
)
from mindroom.metadata_merge import deep_merge_metadata
from mindroom.pre_model_preparation import build_agent_off_loop, prepare_prompt_branches
from mindroom.response_turn import (
    AttemptResolved,
    CompletedAttempt,
    HandledAttempt,
    ResponsePausedForApproval,
    ResponseTurnContext,
    TurnAdapter,
    TurnContinuationRunState,
    TurnPartialSnapshot,
    TurnResume,
    TurnSinks,
    build_llm_request_log_context,
    build_matrix_run_metadata,
    collect_streamed_response_content,
    is_empty_reply,
    pause_without_approval_error,
    paused_attempt_from_response,
    replays_as_turn,
    skip_unapproved_attempt,
    stream_response_turn,
    stream_with_llm_request_log_context,
)
from mindroom.run_storage import (
    RunStorage,
    TerminalRecord,
    bind_turn_session,
    persist_into,
    without_terminal_record,
)
from mindroom.timing import DispatchPipelineTiming, emit_timing_event, timed, timed_block, timing_scope
from mindroom.tool_jobs.notices import append_turn_start_job_note
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import (
    CollectedStreamPresentation,
    FailedReply,
    StreamingToolTracker,
    append_stream_text,
)
from mindroom.tool_system.runtime_context import (
    current_plugins,
    detached_tool_scope,
    get_tool_runtime_context,
    tool_runtime_context,
)
from mindroom.turn_model import ModelScope, TurnModel, resolve_turn_model

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.agent_loop.spec import AgentSpec
    from mindroom.approval.resume import ResumeDecisions
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.history.request_messages import TurnMessages
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.history.types import PreparedHistoryState, ScopeSummary
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.llm.events import RunEvent
    from mindroom.llm.provider import ModelRequest
    from mindroom.llm.runs import AnyRunRecord, ToolExecution
    from mindroom.llm.usage import RunMetrics
    from mindroom.response_turn import TurnRunState
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

__all__ = [
    "AIStreamChunk",
    "TurnResult",
    "agent_run_metadata_content",
    "ai_response",
    "run_delegated_child_response",
    "stream_agent_response",
]
AIStreamChunk = TextDelta | ReasoningDelta | ToolStarted | ToolCompleted | RunFinished | FailedReply


def _compose_current_turn_prompt(
    *,
    raw_prompt: str,
    model_prompt: str | None,
) -> str:
    """Build the current-turn user message, which holds no transient context."""
    prompt_chunks: list[str] = []
    if raw_prompt:
        prompt_chunks.append(raw_prompt)
    model_prompt_tail = _model_prompt_tail_after_raw_prompt(raw_prompt=raw_prompt, model_prompt=model_prompt)
    if model_prompt_tail:
        prompt_chunks.append(model_prompt_tail)

    return "\n\n".join(prompt_chunks)


def _model_prompt_tail_after_raw_prompt(*, raw_prompt: str, model_prompt: str | None) -> str:
    """Return model-only additions after the raw user prompt prefix."""
    if not model_prompt:
        return ""
    model_prompt_tail = strip_user_turn_time_prefix(model_prompt)
    normalized_raw_prompt = raw_prompt.strip()
    normalized_model_prompt_tail = model_prompt_tail.strip()
    if raw_prompt and normalized_model_prompt_tail == normalized_raw_prompt:
        return ""
    if raw_prompt and normalized_model_prompt_tail.startswith(f"{normalized_raw_prompt}\n\n"):
        return normalized_model_prompt_tail[len(normalized_raw_prompt) + 2 :].lstrip()
    return model_prompt_tail


def _initial_agent_continuation(ctx: ResponseTurnContext) -> TurnContinuationRunState:
    """Build the initial continuation state for one agent response turn."""
    return TurnContinuationRunState.initial(
        prompt=ctx.prompt,
        model_prompt=ctx.model_prompt,
        current_timestamp_ms=ctx.current_timestamp_ms,
        current_prompt_is_structured=ctx.current_prompt_is_structured,
        current_event_id=ctx.reply_to_event_id,
        run_id=ctx.run_id,
        continuation_model_prompt_tail=_model_prompt_tail_after_raw_prompt(
            raw_prompt=ctx.prompt,
            model_prompt=ctx.model_prompt,
        ),
    )


@dataclass(frozen=True)
class _PreparedAgentRun:
    """Prepared agent invocation state after history planning.

    ``spec`` is the build's spec with this turn's system messages, stable then turn; ``history`` and ``summary``
    are the stored history the request replays under the resolved plan, and ``turn`` is the new input around the
    prompt.
    ``cli_context`` holds the context documents a minimal turn's Bash reads through `mindroom-agent`.
    """

    build: AgentBuild
    spec: AgentSpec
    turn: TurnMessages
    history: tuple[Message, ...]
    summary: ScopeSummary | None
    unseen_event_ids: list[str]
    prepared_history: PreparedHistoryState
    # Configured model name resolved at preparation time. Run metadata must use
    # this snapshot instead of re-resolving, because the per-thread override
    # store can change mid-run (for example via the thread_model tool).
    runtime_model_name: str
    cli_context: Mapping[str, str]

    @property
    def prompt_text(self) -> str:
        """Return the prompt-visible text derived from the turn's messages."""
        return render_prepared_messages_text(self.turn.in_request_order())

    def run_input(self, media: MediaInputs) -> list[Message]:
        """Return one attempt's request body, media on the prompt: the summary, the replayed history, then the turn."""
        return build_request(summary=self.summary, history=self.history, turn=self.turn, media=media)


@dataclass(frozen=True)
class _AgentAttempt:
    """Inputs and identity for one agent run attempt, and the partial output it has shown."""

    run_input: list[Message]
    run_id: str
    stream_state: _StreamingAttemptState
    # The metadata its run's record stores: the turn's, with the usage of the attempts the turn spent before it.
    metadata: dict[str, Any] | None

    def partial_snapshot(self) -> TurnPartialSnapshot:
        """Return what an interruption records of this attempt: its partial output, its run id, and its run's input.

        The input is the one the run stored, once it stored a record.
        """
        state = self.stream_state
        record = state.terminal.record
        return TurnPartialSnapshot(
            assistant_text=state.assistant_text,
            completed_tools=tuple(state.completed_tools),
            interrupted_tools=tuple(pending.trace_entry for pending in state.pending_tools),
            attempt_run_id=self.run_id,
            turn_input=stored_turn_input(record.messages) if record is not None else None,
        )


_render_system_enrichment_context = timed("system_prompt_assembly.system_enrichment_render")(
    render_system_enrichment_block,
)


@dataclass
class _StreamingAttemptState:
    assistant_text: str = ""
    observed_tool_calls: int = 0
    tool_tracker: StreamingToolTracker = field(default_factory=StreamingToolTracker)
    terminal: TerminalRecord[RunRecord] = field(default_factory=partial(TerminalRecord, RunRecord))
    finished: bool = False
    first_token_logged: bool = False
    error: Exception | None = None

    @property
    def pending_tools(self) -> list[Any]:
        return self.tool_tracker.pending_tools

    @property
    def completed_tools(self) -> list[ToolTraceEntry]:
        return self.tool_tracker.completed_tools


def _agent_turn_adapter(
    *,
    agent_name: str,
    user_message: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
    run_attempt: Callable[
        [TurnRunState, TurnContinuationRunState],
        AsyncIterator[AIStreamChunk | AttemptResolved],
    ],
    error_text: Callable[[Exception], str],
) -> TurnAdapter[AIStreamChunk]:
    """Return the turn driver's adapter for one agent response: its scope, its attempts, and its error reply."""

    def _load_scope() -> ScopeSessionContext:
        return load_scope_session(
            scope=HistoryScope(kind="agent", scope_id=agent_name),
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
        )

    return TurnAdapter[AIStreamChunk](
        load_scope=_load_scope,
        run_attempt=run_attempt,
        make_text_chunk=TextDelta,
        user_message=user_message,
        unexpected_error_text=error_text,
    )


@dataclass(frozen=True)
class _AgentRunContext:
    """Prepared state shared by one top-level agent response lifecycle."""

    # The attempt's turn: its prompt, model, and enrichment as this attempt runs them.
    turn: ResponseTurnContext
    # turn.session_id narrowed to non-None once at the entry boundary; read
    # this field, not turn.session_id, wherever a str is required.
    session_id: str
    prepared_run: _PreparedAgentRun
    metadata: dict[str, Any] | None
    # The settings of every request of this run: the build's, with the turn's native history filled in.
    request_template: ModelRequest
    config: Config
    runtime_paths: RuntimePaths
    # The turn's record, where the run's mid-turn compaction notes the calls a fold summarized.
    turn_recorder: TurnRecorder

    @property
    def agent_name(self) -> str:
        """Return the agent name; agent turns label the context with it."""
        return self.turn.entity_label


def agent_run_metadata_content(
    config: Config,
    record: AnyRunRecord | None,
    *,
    model_name: str,
    run_id: str,
    session_id: str,
    status: RunStatus,
    prepared_history: PreparedHistoryState | None,
    prior_usage: Sequence[RunMetrics] = (),
) -> dict[str, Any]:
    """Return the run metadata an agent reply or approval resume publishes for its final run ``record``.

    ``prepared_history`` is the history the run was prepared with, None for an approval resume, which prepares none.
    ``prior_usage`` is the usage of the turn's attempts before it, which the reply's usage adds. The tool calls
    counted are the ones the record holds: a run that reported a tool call hands over its record however it ends.
    """
    return build_ai_run_metadata_content(
        config=config,
        prices=model_prices(config),
        model_name=model_name,
        run_id=run_id,
        session_id=session_id,
        status=status,
        model=record.model if record is not None else None,
        model_provider=record.model_provider if record is not None else None,
        metrics=record.metrics if record is not None else None,
        prior_usage=prior_usage,
        context_run=record,
        tool_count=len(record.tools or ()) if record is not None else 0,
        prepared_history=prepared_history,
    )


def _attempt_run_metadata_builder(
    run_context: _AgentRunContext,
    record: AnyRunRecord | None,
    status: RunStatus,
    *,
    run_id: str,
    prior_usage: tuple[RunMetrics, ...],
) -> Callable[[], dict[str, Any]]:
    """Return how to build the run metadata one ended attempt publishes, from inputs that no longer change.

    ``prior_usage`` is the usage of the turn's attempts before this one, which it publishes if it ends the turn.
    """
    prepared = run_context.prepared_run
    return partial(
        agent_run_metadata_content,
        run_context.config,
        record,
        model_name=prepared.runtime_model_name,
        run_id=run_id,
        session_id=run_context.session_id,
        status=status,
        prepared_history=prepared.prepared_history,
        prior_usage=prior_usage,
    )


def _track_stream_tool_started(state: _StreamingAttemptState, event: ToolStarted) -> bool:
    """Record a started tool call's trace; return False for a call the stream already announced."""
    _, trace_entry = state.tool_tracker.start(event.call, origin=event.origin)
    if trace_entry is None:
        return False
    state.observed_tool_calls += 1
    return True


def _materialize_paused_agent_tool_events(
    state: _StreamingAttemptState,
    tools: Sequence[ToolExecution],
) -> tuple[ToolStarted, ...]:
    """Return the tool-start events of paused calls the stream has not announced yet."""
    events = (ToolStarted(tool.call) for tool in tools)
    return tuple(event for event in events if _track_stream_tool_started(state, event))


def _stream_completed_without_visible_output(state: _StreamingAttemptState, record: AnyRunRecord) -> bool:
    visible_text = state.assistant_text.strip() or (record.content or "").strip()
    return not visible_text and state.observed_tool_calls == 0


def _is_empty_reply(state: _StreamingAttemptState, record: AnyRunRecord) -> bool:
    """Whether the attempt's run is an empty reply (``is_empty_reply``) whose stream showed no text either.

    A provider's final message can hold less than it streamed, and the user saw what streamed. A run whose events reach
    the attempt directly hands its record to its persist callback once they have passed the attempt, so the store
    reads what the resolution reads; a queue between the run and the attempt, such as the agent CLI's merged stream or
    the delegation driver's continuation stream, can still hold the last of them, and then the record's content, which
    holds every text an adapter finished with, decides.
    """
    return is_empty_reply(record) and _stream_completed_without_visible_output(state, record)


def _attempt_request_log_context(run_context: _AgentRunContext) -> dict[str, object]:
    """Build request-log context for the exact prompt used by one provider attempt."""
    ctx = run_context.turn
    return build_llm_request_log_context(
        RequestAttribution(
            agent_id=ctx.entity_label,
            session_id=run_context.session_id,
            room_id=ctx.room_id,
            thread_id=ctx.thread_id,
            reply_to_event_id=ctx.reply_to_event_id,
            requester_id=ctx.requester_id,
            correlation_id=ctx.correlation_id,
        ),
        prompt=ctx.prompt,
        model_prompt=ctx.model_prompt,
        full_prompt=run_context.prepared_run.prompt_text,
        metadata=run_context.metadata,
    )


def _begin_attempt(
    run: TurnRunState,
    run_context: _AgentRunContext,
    continuation: TurnContinuationRunState,
    media: MediaInputs,
    *,
    stream_state: _StreamingAttemptState,
) -> tuple[RunStorage, _AgentAttempt]:
    """Record one prepared attempt on the turn's run state, and return where it is stored and its request."""
    prepared_run = run_context.prepared_run
    run.run_metadata = run_context.metadata
    run.turn_input = TurnInput(context=prepared_run.turn.context)
    storage = bind_turn_session(
        run,
        replays=lambda record: replays_as_turn(
            run_context.turn,
            record,
            is_empty=_is_empty_reply(stream_state, record),
        ),
    )
    attempt = run.attempt = _AgentAttempt(
        run_input=prepared_run.run_input(media),
        run_id=continuation.active_run_id or str(uuid4()),
        stream_state=stream_state,
        metadata=with_prior_usage(run_context.metadata, run.prior_usage),
    )
    return storage, attempt


def _agent_loop_hooks(
    run_context: _AgentRunContext,
    storage: RunStorage,
) -> LoopHooks:
    """Return the hooks of one agent run: the turn's gate, lifecycle, recorder, and skill capture around the shared set.

    A fold appends a summary row to the scope only when this request replays the scope's stored history; a turn
    that carries none (a scheduled turn whose budget disabled replay) keeps its summary as a message of its own run.
    """
    ctx = run_context.turn
    prepared = run_context.prepared_run
    return agent_loop_hooks(
        prepared.build,
        HookTurn(
            config=run_context.config,
            runtime_paths=run_context.runtime_paths,
            model_name=prepared.runtime_model_name,
            storage=storage,
            replay_plan=prepared.prepared_history.replay_plan,
            prompt_id=prepared.turn.prompt.id,
            lifecycle=ctx.compaction_lifecycle,
            turn_recorder=run_context.turn_recorder,
        ),
        participation=ctx.participation,
        skill_review_capture=ctx.skill_review_capture,
    )


async def _start_agent_run(
    run_context: _AgentRunContext,
    attempt: _AgentAttempt,
    *,
    storage: RunStorage,
    terminal: TerminalRecord[RunRecord],
) -> AsyncIterator[RunEvent]:
    """Run one attempt on the agent loop, storing its terminal record where the turn's scope stores.

    A run that pauses on delegations is driven on: its children run, and the run continues with their results
    through ``_TurnRunContinuation`` until it completes or waits for an approval. An agent whose shell reaches its
    other tools through `mindroom-agent` binds this attempt's CLI first; a minimal turn then offers only Bash, and
    the hidden calls' tool events join the run's own.
    """
    prepared = run_context.prepared_run
    build = prepared.build
    run = RunContext(
        run_id=attempt.run_id,
        user_id=run_context.turn.requester_id,
        metadata=attempt.metadata,
        session=storage.session,
        model_name=prepared.runtime_model_name,
    )
    spec = prepared.spec
    hooks = _agent_loop_hooks(run_context, storage)
    if build.surface.mode == "minimal":
        spec, hooks = await minimal_attempt(
            build,
            spec,
            hooks,
            run=run,
            storage=storage,
            response_context=run_context.turn,
            context_documents=prepared.cli_context,
            delegation_depth=run_context.turn.delegation_depth,
            refresh_scheduler=run_context.turn.refresh_scheduler,
        )
    elif build.surface.cli_shell:
        await bind_standard_cli(
            build,
            run=run,
            storage=storage,
            response_context=run_context.turn,
            delegation_depth=run_context.turn.delegation_depth,
            refresh_scheduler=run_context.turn.refresh_scheduler,
        )
    events = run_agent(
        spec,
        attempt.run_input,
        ctx=run,
        hooks=hooks,
        persist=persist_into(terminal, storage),
        request_template=run_context.request_template,
        metadata=run.metadata,
    )
    driven = drive_delegation_stream(
        events,
        record_type=RunRecord,
        continue_parent=_TurnRunContinuation(run_context, spec=spec, hooks=hooks, storage=storage, terminal=terminal),
        agent_name=run_context.agent_name,
        run_child=run_delegated_child_response,
        config=run_context.config,
        runtime_paths=run_context.runtime_paths,
        execution_identity=run_context.turn.execution_identity,
        delegation_depth=run_context.turn.delegation_depth,
        refresh_scheduler=run_context.turn.refresh_scheduler,
    )
    if build.surface.mode == "minimal" or build.surface.cli_shell:
        driven = stream_cli_events(driven)
    async with closing_async_stream(driven):
        async for event in driven:
            yield event


@dataclass(frozen=True)
class _TurnRunContinuation:
    """Continues one attempt's paused run with its delegations' results, as the spec and hooks it ran with.

    The run keeps its hooks for its whole life, so per-run hook state, such as the output-limit count or a failed
    compaction's stop, carries across its continuations. The resume replays the scope's history as the attempt did
    and reuses the paused run's stored system messages.
    """

    run_context: _AgentRunContext
    spec: AgentSpec
    hooks: LoopHooks
    storage: RunStorage
    terminal: TerminalRecord[RunRecord]

    def events(self, paused: RunRecord, decisions: ResumeDecisions) -> AsyncIterator[RunEvent]:
        """Resume ``paused`` with ``decisions``; the stream ends as ``resume_from_session``'s does."""
        prepared = self.run_context.prepared_run
        return resume_from_session(
            self.spec,
            paused,
            decisions,
            storage=self.storage,
            settings=prepared.build.history_settings,
            plan=prepared.prepared_history.replay_plan,
            user_id=self.run_context.turn.requester_id,
            model_name=prepared.runtime_model_name,
            hooks=self.hooks,
            persist=persist_into(self.terminal, self.storage),
            request_template=self.run_context.request_template,
        )


def _assert_agent_target(agent_name: str, config: Config) -> None:
    """Reject configured team names in the agent-only AI helper path."""
    if agent_name in config.teams:
        msg = (
            f"'{agent_name}' is a configured team, not an agent. "
            "Use the explicit team execution helpers or the OpenAI-compatible model "
            f"'team/{agent_name}' instead."
        )
        raise ValueError(msg)


def _turn_session_id(ctx: ResponseTurnContext) -> str:
    """Return the turn's session id, which agent response turns always run with, and bind the turn's timing scope.

    The timing scope is one short identifier for correlating AI timing logs; asyncio task contexts isolate it, and
    the next turn in a reused task overwrites it.
    """
    if not ctx.session_id:
        msg = "agent response turns require ResponseTurnContext.session_id"
        raise ValueError(msg)
    timing_scope.set((ctx.reply_to_event_id or ctx.run_id or ctx.session_id)[:20])
    return ctx.session_id


def _mark_pipeline_timing(pipeline_timing: DispatchPipelineTiming | None, label: str) -> None:
    """Record one dispatch timing mark when turn-level timing is available."""
    if pipeline_timing is not None:
        pipeline_timing.mark(label)


def _turn_system_messages(build: AgentBuild, ctx: ResponseTurnContext, session_preamble: str) -> tuple[Message, ...]:
    """Render the turn's system messages: the build's prompt, the memory preamble, and the turn's system enrichment."""
    return prompt_for_turn(
        build,
        session_preamble,
        _render_system_enrichment_context(ctx.system_enrichment_items) if ctx.system_enrichment_items else "",
    )


def _memory_conversation(
    ctx: ResponseTurnContext,
    config: Config,
    scope_context: ScopeSessionContext | None,
) -> ConversationRef | None:
    """Name the conversation whose cached system prompt the turn's inlined MEMORY.md should keep unchanged.

    Only file memory inlines a MEMORY.md. An authored persona recalls no memory, a minimal agent reads its memory on
    demand rather than in its system prompt, and a replay limit, configured or scheduled, moves the history after the
    system prompt every turn and can drop the agent's own memory writes, so none of them has a cache to keep.
    Without automatic compaction, fitting a long conversation into the context window is such a limit.
    """
    if (
        ctx.persona is not None
        or ctx.agent_mode == "minimal"
        or ctx.scheduled_history_budget is not None
        or scope_context is None
    ):
        return None
    entity = config.resolve_scope(ctx.entity_label)
    settings = entity.history_settings
    if entity.memory_backend != "file" or not settings.replays_whole_history or not settings.automatic_compaction:
        return None
    session = scope_context.session
    compaction = compaction_marker(session) if session is not None else (None, None)
    return ConversationRef(scope_context.session_id, compaction)


async def _prepare_turn_memory(
    ctx: ResponseTurnContext,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation: ConversationRef | None,
) -> MemoryPromptParts:
    """Recall memories for a configured agent turn; an authored persona recalls none."""
    if ctx.persona is not None:
        return MemoryPromptParts()
    return await build_memory_prompt_parts(
        ctx.prompt,
        ctx.entity_label,
        config,
        runtime_paths,
        execution_identity=ctx.execution_identity,
        conversation=conversation,
    )


async def _system_messages_after_compaction(
    system_messages: tuple[Message, ...],
    build: AgentBuild,
    ctx: ResponseTurnContext,
    config: Config,
    runtime_paths: RuntimePaths,
    scope_context: ScopeSessionContext | None,
    conversation: ConversationRef | None,
) -> tuple[Message, ...]:
    """Render the system messages again with the current MEMORY.md when this turn's history preparation compacted.

    That compaction already changes the request after the system prompt, so reloading here costs nothing, while
    waiting for the next turn would make it miss the cache on the whole compacted history.
    """
    compacted = _memory_conversation(ctx, config, scope_context)
    if compacted is None or compacted == conversation:
        return system_messages
    fresh = await conversation_entrypoint_preamble(
        ctx.entity_label,
        config,
        runtime_paths,
        ctx.execution_identity,
        conversation=compacted,
    )
    return _turn_system_messages(build, ctx, fresh)


@timed("system_prompt_assembly")
async def _prepare_agent_and_prompt(
    ctx: ResponseTurnContext,
    *,
    runtime_paths: RuntimePaths,
    config: Config,
    scope_context: ScopeSessionContext | None,
    current_event_id: str | None,
) -> _PreparedAgentRun:
    """Prepare the agent build, this turn's system messages (stable, then turn), and its request input.

    Returns the prepared run input plus history bookkeeping for one agent turn.
    """
    agent_name = ctx.entity_label
    prompt = ctx.prompt
    execution_identity = ctx.execution_identity
    pipeline_timing = ctx.pipeline_timing

    def _resolve_model_and_build_agent() -> tuple[TurnModel, AgentBuild]:
        """Resolve the runtime model and build the agent off the event loop (#1260).

        Both walk the filesystem per dispatch: thread-model overrides, workspace
        scaffolding, and context-file reads.
        """
        runtime_model = resolve_turn_model(
            config,
            runtime_paths,
            ModelScope(entity=agent_name, room_id=ctx.room_id, thread_id=ctx.thread_id, explicit=ctx.active_model_name),
            None,
        )
        build = ctx.reusable_build
        if build is None:
            build = create_agent(
                agent_name,
                config,
                runtime_paths,
                model=runtime_model,
                knowledge=ctx.knowledge,
                plugins=current_plugins(config, runtime_paths),
                include_interactive_questions=ctx.include_interactive_questions,
                tool_function_filter=ctx.tool_function_filter,
                include_openai_compat_guidance=ctx.include_openai_compat_guidance,
                execution_identity=execution_identity,
                delegation_depth=ctx.delegation_depth,
                refresh_scheduler=ctx.refresh_scheduler,
                supports_native_tool_approval=ctx.supports_native_tool_approval,
                eager_deferred_tools=ctx.eager_deferred_tools,
                agent_mode=ctx.agent_mode,
                agent_cli_in_shell=True,
                persona=ctx.persona,
                tool_names=ctx.tool_names,
            )
        return runtime_model, build

    memory_backend = agent_memory_backend(config, agent_name)
    memory_conversation = _memory_conversation(ctx, config, scope_context)
    parallel_branches = memory_backend == "mem0"
    serial_reason: str | None = None
    if memory_backend == "file":
        parallel_branches = ctx.reusable_build is not None or await asyncio.to_thread(
            agent_build_can_overlap_file_memory,
            agent_name,
            config,
            runtime_paths.storage_root,
        )
        if not parallel_branches:
            serial_reason = "default_workspace_scaffold_pending"
    if pipeline_timing is not None:
        pipeline_timing.note(
            prompt_branches_parallel=parallel_branches,
            prompt_branches_memory_backend=memory_backend,
            prompt_branches_serial_reason=serial_reason,
        )
    current_turn_prompt = _compose_current_turn_prompt(raw_prompt=prompt, model_prompt=ctx.model_prompt)
    if parallel_branches:
        _mark_pipeline_timing(pipeline_timing, "prompt_branches_start")
        with timed_block(
            "system_prompt_assembly.memory_agent_join",
            parallel=True,
            memory_backend=memory_backend,
        ):
            try:
                prompt_parts, runtime_model, build = await prepare_prompt_branches(
                    prepare_memory=partial(_prepare_turn_memory, ctx, config, runtime_paths, memory_conversation),
                    build_agent=_resolve_model_and_build_agent,
                    agent_name=agent_name,
                    pipeline_timing=pipeline_timing,
                )
            finally:
                _mark_pipeline_timing(pipeline_timing, "prompt_branches_ready")
    else:
        _mark_pipeline_timing(pipeline_timing, "memory_prepare_start")
        prompt_parts = await _prepare_turn_memory(ctx, config, runtime_paths, memory_conversation)
        _mark_pipeline_timing(pipeline_timing, "memory_prepare_ready")
        _mark_pipeline_timing(pipeline_timing, "agent_build_start")
        runtime_model, build = await build_agent_off_loop(_resolve_model_and_build_agent, agent_name=agent_name)
        _mark_pipeline_timing(pipeline_timing, "agent_build_ready")

    cli_context: Mapping[str, str] = {}
    if build.surface.mode == "minimal":
        minimal = minimal_turn(
            build,
            session_preamble=prompt_parts.session_preamble,
            transient_memory=prompt_parts.transient_turn_context,
            system_enrichment=ctx.system_enrichment_items,
            transient_enrichment=ctx.transient_enrichment_items,
        )
        system_messages, transient_turn_context, cli_context = (
            minimal.system_messages,
            minimal.transient_context,
            minimal.context_documents,
        )
    else:
        system_messages = _turn_system_messages(build, ctx, prompt_parts.session_preamble)
        transient_turn_context = render_transient_context(
            (prompt_parts.transient_turn_context, render_enrichment_block(list(ctx.transient_enrichment_items))),
        )
    prepared_execution = await prepare_agent_execution_context(
        ctx,
        scope_context=scope_context,
        # A minimal turn's requests offer only Bash, so its history budget is sized with Bash alone.
        build=presented_build(build) if build.surface.mode == "minimal" else build,
        system_messages=system_messages,
        prompt=current_turn_prompt,
        transient_context_messages=(
            (Message(role="user", content=transient_turn_context, add_to_agent_memory=False),)
            if transient_turn_context
            else ()
        ),
        thread_history=ctx.thread_history,
        runtime_paths=runtime_paths,
        config=config,
        resolved_runtime_model=runtime_model,
        compaction_lifecycle=ctx.compaction_lifecycle,
        current_sender_id=None if ctx.include_openai_compat_guidance else ctx.current_sender_id or ctx.requester_id,
        current_timestamp_ms=ctx.current_timestamp_ms,
        current_event_id=current_event_id,
        current_prompt_is_structured=ctx.current_prompt_is_structured,
        include_openai_compat_guidance=ctx.include_openai_compat_guidance,
        pipeline_timing=pipeline_timing,
    )
    system_messages = await _system_messages_after_compaction(
        system_messages,
        build,
        ctx,
        config,
        runtime_paths,
        scope_context,
        memory_conversation,
    )
    history, summary = (
        replayed_history(
            scope_context.session,
            scope_context.scope,
            build.history_settings,
            prepared_execution.replay_plan,
        )
        if scope_context is not None
        else ([], None)
    )
    turn = prepared_execution.turn

    # Routine logs stay content-safe; detailed prompt text is emitted separately only at DEBUG.
    logger.info(
        "Preparing agent and prompt",
        agent=agent_name,
        message_count=len(turn.in_request_order()),
        history_message_count=len(history),
        unseen_event_count=len(prepared_execution.unseen_event_ids),
    )
    logger.debug(
        "Prepared agent full prompt",
        agent=agent_name,
        full_prompt=render_prepared_messages_text(turn.in_request_order()),
    )
    return _PreparedAgentRun(
        build=build,
        spec=replace(build.spec, system_messages=system_messages),
        turn=turn,
        history=tuple(history),
        summary=summary,
        unseen_event_ids=prepared_execution.unseen_event_ids,
        prepared_history=prepared_execution.prepared_history,
        runtime_model_name=runtime_model.model_name,
        cli_context=cli_context,
    )


async def _prepare_agent_run_context(
    ctx: ResponseTurnContext,
    continuation: TurnContinuationRunState,
    *,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    scope_context: ScopeSessionContext | None,
    turn_recorder: TurnRecorder,
) -> _AgentRunContext:
    """Prepare one attempt of an agent response, as its ``continuation`` state asks, through metadata assembly."""
    ctx = replace(
        ctx,
        prompt=continuation.active_prompt,
        model_prompt=continuation.active_model_prompt,
        current_timestamp_ms=continuation.active_current_timestamp_ms,
        current_prompt_is_structured=continuation.active_current_prompt_is_structured,
        active_model_name=continuation.active_model_name or ctx.active_model_name,
    )
    pipeline_timing = ctx.pipeline_timing
    if pipeline_timing is not None:
        pipeline_timing.mark("ai_prepare_start", overwrite=True)
    if ctx.delegation_depth == 0 and ctx.agent_mode != "minimal":
        # Only a top-level standard reply can collect jobs, so only it hears which ended.
        ctx = replace(ctx, transient_enrichment_items=await append_turn_start_job_note(ctx.transient_enrichment_items))
    prepared_run = await _prepare_agent_and_prompt(
        ctx,
        runtime_paths=runtime_paths,
        config=config,
        scope_context=scope_context,
        current_event_id=continuation.active_current_event_id,
    )
    if pipeline_timing is not None:
        pipeline_timing.mark("history_ready", overwrite=True)
        note_prepared_history_timing(pipeline_timing, prepared_run.prepared_history)

    build = prepared_run.build
    request_template = with_native_history(build.request_template, prepared_run.prepared_history.native_history)
    run_extra_content = build_prepared_history_metadata_content(prepared_run.prepared_history)
    metadata = build_matrix_run_metadata(
        ctx.reply_to_event_id,
        prepared_run.unseen_event_ids,
        room_id=ctx.room_id,
        thread_id=ctx.thread_id,
        requester_id=ctx.requester_id,
        correlation_id=ctx.correlation_id,
        extra_metadata=deep_merge_metadata(without_run_usage(ctx.matrix_run_metadata), run_extra_content),
    )
    if ctx.agent_mode == "minimal":
        metadata = dict(metadata or {}) | {"agent_mode": "minimal"}

    return _AgentRunContext(
        turn=ctx,
        session_id=session_id,
        prepared_run=prepared_run,
        metadata=metadata,
        request_template=request_template,
        config=config,
        runtime_paths=runtime_paths,
        turn_recorder=turn_recorder,
    )


async def run_delegated_child_response(
    child: DelegationChild,
    *,
    prompt: str,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    supports_native_tool_approval: bool = True,
    approval_config: Config | None = None,
    prior_usage: Sequence[RunMetrics] = (),
) -> str:
    """Execute the normal response envelope for a prepared child owned by either adapter.

    The native delegation driver resumes a child that pauses for approval; the direct delegate tool, which cannot,
    passes ``supports_native_tool_approval=False``. A minimal child cannot pause for approval: approval-gated tools
    stay hidden from it, so no adapter ever has to resume it natively. ``prior_usage`` is the usage of the child's turn
    before this envelope: a resumed run a model switch continued, whose continuation this envelope runs.
    """
    identity = child_execution_identity(child)
    authorized_config = authorize_delegation(
        child.caller_agent_name,
        child.child_agent_name,
        prompt,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=replace(identity, agent_name=child.caller_agent_name),
        depth=child.depth - 1,
        grant=delegation_grant(child),
    )
    if isinstance(authorized_config, str):
        return authorized_config
    # A caller's pre-approval overlay replaces the live config for the child's run.
    active_config = approval_config if approval_config is not None else authorized_config
    knowledge = await resolve_agent_knowledge_access_async(
        child.child_agent_name,
        active_config,
        runtime_paths,
        refresh_scheduler=refresh_scheduler,
        execution_identity=identity,
        only=child.persona.kept_knowledge if child.persona is not None else None,
    )
    context = get_tool_runtime_context()
    child_context = child_tool_runtime_context(context, child) if context is not None else None
    turn = ResponseTurnContext(
        agent_mode=child.agent_mode,
        persona=child.persona,
        entity_label=child.child_agent_name,
        session_id=child.session_id,
        run_id=child.run_id,
        correlation_id=(child_context.correlation_id if child_context is not None else None) or uuid4().hex,
        reply_to_event_id=None,
        room_id=identity.room_id,
        thread_id=identity.resolved_thread_id,
        requester_id=identity.requester_id,
        matrix_run_metadata=None,
        active_model_name=child.model_name,
        transient_enrichment_items=tuple(append_knowledge_availability_enrichment((), knowledge.unavailable)),
        prompt=prompt,
        show_tool_calls=False,
        execution_identity=identity,
        refresh_scheduler=refresh_scheduler,
        knowledge=knowledge.knowledge,
        include_interactive_questions=False,
        tool_function_filter=context.tool_function_filter if context is not None else None,
        delegation_depth=child.depth,
        supports_native_tool_approval=supports_native_tool_approval and child.agent_mode == "standard",
    )
    sinks = TurnSinks(
        # Delegation recovery records a child's interruption.
        persist_interrupted=False,
        on_attempt_start=lambda run_id, model_name: note_child_run_id(
            child,
            run_id,
            runtime_paths,
            model_name=model_name,
        ),
    )
    # The child's tools run as the child, whichever adapter runs it; its caller's identity stays with the caller.
    with tool_runtime_context(child_context) if child_context is not None else detached_tool_scope(identity):
        result = await ai_response(
            turn,
            runtime_paths,
            active_config,
            sinks=sinks,
            resume=TurnResume(prior_usage=tuple(prior_usage)),
        )
    return result.text


def _with_background_tool_jobs(
    ctx: ResponseTurnContext,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ResponseTurnContext:
    """Run the turn's managed calls as background jobs while the instance runs them, except in a voice call."""
    return replace(
        ctx,
        background_tool_jobs=not ctx.inline_tools and background_tool_jobs_enabled(config, runtime_paths),
    )


@dataclass(frozen=True)
class TurnResult:
    """How one blocking agent turn ended: its reply text, and the attempt that ended it.

    A completed turn holds the attempt that completed it, and a turn that failed with a reply text holds that failure;
    a turn with neither was skipped, or refused before it ran, as one that names a configured team is.
    """

    text: str
    completed: CompletedAttempt | None = None
    failed: HandledAttempt | None = None

    @cached_property
    def run_metadata(self) -> dict[str, Any]:
        """Return the run metadata of the attempt that ended the turn, built when read.

        It names that attempt's model and status, and its usage covers every attempt of the turn. Building it prices
        the usage, so a caller that reads only the text, such as a delegated child's parent, never pays for it.
        """
        ending = self.completed or self.failed
        build = ending.build_run_metadata if ending is not None else None
        return build() if build is not None else {}


async def ai_response(
    ctx: ResponseTurnContext,
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    sinks: TurnSinks | None = None,
    resume: TurnResume | None = None,
) -> TurnResult:
    """Generate one agent response and return how its turn ended, its text collected from the turn's stream.

    ``sinks`` says how the turn records itself; the result holds the attempt that ended the turn, so any run metadata
    collector, completion, or failure sink ``sinks`` names is replaced. ``resume`` is where the turn starts, as
    ``stream_agent_response`` takes it.
    """
    completed: list[CompletedAttempt] = []
    failed: list[HandledAttempt] = []
    reporting = replace(
        sinks or TurnSinks(),
        run_metadata_collector=None,
        on_completed=completed.append,
        on_failed=failed.append,
    )
    body, _tool_trace = await collect_streamed_response_content(
        stream_agent_response(ctx, runtime_paths, config, sinks=reporting, resume=resume),
        # Hidden tool calls still separate the text around them, as a streamed reply shows it.
        presentation=CollectedStreamPresentation(show_tool_calls=ctx.show_tool_calls, track_hidden_tools=True),
    )
    return TurnResult(
        text=body,
        completed=completed[-1] if completed else None,
        failed=failed[-1] if failed else None,
    )


@timed("model_request_to_completion")
async def _process_stream_events(  # noqa: C901, PLR0912
    events: AsyncIterator[RunEvent],
    *,
    state: _StreamingAttemptState,
    show_tool_calls: bool,
    show_reasoning: bool,
    agent_name: str,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> AsyncGenerator[AIStreamChunk]:
    """Consume one streaming attempt, yielding chunks and mutating *state*."""
    try:
        async for event in events:
            await observe_child_event(event)
            if isinstance(event, ReasoningDelta):
                if show_reasoning and event.text:
                    yield event
                continue
            if isinstance(event, TextDelta):
                if not event.text:
                    continue
                if not state.first_token_logged:
                    state.first_token_logged = True
                    if pipeline_timing is not None:
                        pipeline_timing.mark("model_first_token")
                state.assistant_text += event.text
                yield event
                continue

            if isinstance(event, ToolStarted):
                emit_timing_event(
                    "Dispatch tool-call timing",
                    phase="tool_call_started",
                    agent_name=agent_name,
                    tool_name=event.call.tool_name,
                    tool_call_id=event.call.tool_call_id,
                    show_tool_calls=show_tool_calls,
                )
                _track_stream_tool_started(state, event)
                yield event
                continue

            if isinstance(event, ToolCompleted):
                emit_timing_event(
                    "Dispatch tool-call timing",
                    phase="tool_call_completed",
                    agent_name=agent_name,
                    tool_name=event.call.tool_name,
                    tool_call_id=event.call.tool_call_id,
                    show_tool_calls=show_tool_calls,
                )
                state.tool_tracker.complete(event.call, origin=event.origin)
                yield event
                continue

            if isinstance(event, RunFinished):
                state.finished = True
                state.terminal.keep(event.record)
                if event.record.status is RunStatus.completed and event.record.content is not None:
                    yield event
                continue

            logger.debug("Skipping stream event", event_type=type(event).__name__)
    except ResponsePausedForApproval:
        raise
    except Exception as e:
        logger.exception("Error during streaming AI response")
        state.error = e


async def _stream_agent_attempt_chunks(
    *,
    run_context: _AgentRunContext,
    attempt: _AgentAttempt,
    storage: RunStorage,
    state: _StreamingAttemptState,
    on_attempt_start: Callable[[str, str], None] | None,
) -> AsyncGenerator[AIStreamChunk]:
    """Start and consume one streaming agent attempt."""
    show_tool_calls = run_context.turn.show_tool_calls
    pipeline_timing = run_context.turn.pipeline_timing
    try:
        if on_attempt_start is not None:
            on_attempt_start(attempt.run_id, run_context.prepared_run.runtime_model_name)
        request_context = _attempt_request_log_context(run_context)
        with bind_llm_request_log_context(**request_context):
            if pipeline_timing is not None:
                pipeline_timing.mark_model_request()
            run_events = _start_agent_run(run_context, attempt, storage=storage, terminal=state.terminal)
        events = stream_with_llm_request_log_context(run_events, request_context=request_context)
        chunks = _process_stream_events(
            events,
            state=state,
            show_tool_calls=show_tool_calls,
            show_reasoning=run_context.turn.show_reasoning,
            agent_name=run_context.agent_name,
            pipeline_timing=pipeline_timing,
        )
        async with closing_async_stream(run_events), closing_async_stream(events), aclosing(chunks):
            async for stream_chunk in chunks:
                yield stream_chunk
    except ResponsePausedForApproval:
        raise
    except Exception as e:
        logger.exception("Error starting streaming AI response")
        state.error = e


async def stream_agent_response(  # noqa: C901, PLR0915
    ctx: ResponseTurnContext,
    runtime_paths: RuntimePaths,
    config: Config,
    *,
    sinks: TurnSinks | None = None,
    resume: TurnResume | None = None,
) -> AsyncIterator[AIStreamChunk]:
    """Generate one streaming agent response on the agent loop, yielding chunks and events as they become available.

    ``ctx`` carries every input of the turn (``entity_label`` names the agent, ``session_id`` is required, and
    ``requester_id`` is the Matrix sender used for prompt attribution); ``sinks`` receives what the turn writes back,
    and ``resume`` restores an attempt to settle, or counts the same-turn attempts spent and their usage, before the
    first fresh request.
    """
    ctx = _with_background_tool_jobs(ctx, config, runtime_paths)
    sinks = sinks or TurnSinks()
    agent_name = ctx.entity_label
    # The agent's reply to an error, its credentials redacted.
    error_text = partial(get_user_friendly_error_message, agent_name=agent_name, runtime_paths=runtime_paths)
    logger.info("AI request", agent=agent_name, room_id=ctx.room_id)
    session_id = _turn_session_id(ctx)
    media_inputs = ctx.media or MediaInputs()
    show_tool_calls = ctx.show_tool_calls
    try:
        _assert_agent_target(agent_name, config)
    except ValueError as e:
        yield FailedReply(error_text(e), e)
        return

    async def _run_attempt(  # noqa: C901
        run: TurnRunState,
        continuation_state: TurnContinuationRunState,
    ) -> AsyncGenerator[AIStreamChunk | AttemptResolved]:
        """Stream one agent attempt, ending with its ``AttemptResolved`` sentinel."""
        try:
            run_context = await _prepare_agent_run_context(
                ctx,
                continuation_state,
                session_id=session_id,
                runtime_paths=runtime_paths,
                config=config,
                scope_context=run.scope_context,
                turn_recorder=run.record,
            )
        except Exception as e:
            logger.exception("Error preparing agent", agent=agent_name)
            if skipped := skip_unapproved_attempt(ctx.participation, reason="preparation_failed"):
                yield AttemptResolved(skipped)
                return
            yield AttemptResolved(HandledAttempt(FailedReply(error_text(e), e)))
            return
        prepared_run = run_context.prepared_run
        state = _StreamingAttemptState()
        storage, attempt = _begin_attempt(run, run_context, continuation_state, media_inputs, stream_state=state)
        attempt_stream = _stream_agent_attempt_chunks(
            run_context=run_context,
            attempt=attempt,
            storage=storage,
            state=state,
            on_attempt_start=sinks.on_attempt_start,
        )
        async with closing_async_stream(attempt_stream):
            async for stream_chunk in attempt_stream:
                yield stream_chunk

        record = state.terminal.record
        if state.error is not None:
            if skipped := skip_unapproved_attempt(ctx.participation, reason="run_failed_before_decision"):
                yield AttemptResolved(skipped)
            else:
                failure_text = error_text(state.error)
                # Below the text this attempt already streamed, the error starts a paragraph of its own.
                shown = state.assistant_text
                yield AttemptResolved(
                    HandledAttempt(
                        FailedReply(append_stream_text(shown, failure_text, separate=True)[len(shown) :], state.error),
                        build_run_metadata=_attempt_run_metadata_builder(
                            run_context,
                            record,
                            RunStatus.error,
                            run_id=attempt.run_id,
                            prior_usage=run.prior_usage,
                        ),
                    ),
                )
            return
        if record is None or not state.finished:
            raise without_terminal_record(RunRecord, attempt.run_id)

        if skipped := skip_unapproved_attempt(ctx.participation, reason="participation_declined"):
            yield AttemptResolved(skipped)
            return

        if record.status is RunStatus.paused:
            paused_attempt = paused_attempt_from_response(
                record,
                toolkit_owners=tool_owners_by_agent(prepared_run.build),
            )
            if paused_attempt is None:
                raise pause_without_approval_error(record, entity_label="agent")
            for tool_event in _materialize_paused_agent_tool_events(state, paused_attempt.tools):
                yield tool_event
            yield AttemptResolved(
                replace(
                    paused_attempt,
                    runtime_model_name=prepared_run.runtime_model_name,
                    tool_trace=(
                        paused_attempt.tool_trace
                        if show_tool_calls
                        else tuple(pending.trace_entry for pending in state.pending_tools)
                    ),
                ),
            )
            return

        completed_without_output = _stream_completed_without_visible_output(state, record)
        final_status = RunStatus.error if completed_without_output else RunStatus.completed
        final_text = record.content or ""
        yield AttemptResolved(
            CompletedAttempt(
                status=final_status,
                replayable_text=state.assistant_text or final_text,
                has_visible_content=bool(state.assistant_text or final_text),
                is_empty=_is_empty_reply(state, record),
                run_id=record.run_id,
                attempt_run_id=attempt.run_id,
                usage=record.metrics,
                tool_executions=tuple(record.tools or ()),
                control_calls=cli_control_calls(attempt.run_id),
                completed_tools=tuple(state.completed_tools),
                build_run_metadata=_attempt_run_metadata_builder(
                    run_context,
                    record,
                    final_status,
                    run_id=record.run_id,
                    prior_usage=run.prior_usage,
                ),
            ),
        )

    adapter = _agent_turn_adapter(
        agent_name=agent_name,
        user_message=_compose_current_turn_prompt(raw_prompt=ctx.prompt, model_prompt=ctx.model_prompt),
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=ctx.execution_identity,
        run_attempt=_run_attempt,
        error_text=error_text,
    )
    response_stream = stream_response_turn(
        ctx,
        adapter,
        sinks,
        resume=resume,
        continuation=_initial_agent_continuation(ctx),
    )
    # Close the driver generator deterministically when this wrapper unwinds so
    # its cleanup does not wait for event-loop async-generator finalization.
    async with aclosing(response_stream) as closing_stream:
        async for chunk in closing_stream:
            yield chunk
