"""AI integration module for MindRoom agents and memory management."""

from __future__ import annotations

import asyncio
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom import ai_runtime
from mindroom.agent_cli.cli_shell_agent import bind_standard_cli
from mindroom.agent_cli.events import stream_cli_events
from mindroom.agent_cli.lifetime import cli_control_executions
from mindroom.agent_cli.minimal_agent import minimal_attempt, minimal_turn, presented_build
from mindroom.agent_loop.build import prompt_for_turn, tool_owners_by_agent
from mindroom.agent_loop.run import own_answers, run_agent
from mindroom.agent_loop.spec import RunContext
from mindroom.agent_run_context import append_knowledge_availability_enrichment
from mindroom.agents import agent_build_can_overlap_file_memory, create_agent
from mindroom.ai_run_metadata import build_ai_run_metadata_content, build_prepared_history_metadata_content
from mindroom.approval.resume import forward_resume, resume_from_session
from mindroom.budgets.pricing import model_prices
from mindroom.delegation.execution import answered_delegation_calls, drive_delegation_stream
from mindroom.delegation.lifecycle import (
    authorize_delegation,
    child_execution_identity,
    child_tool_runtime_context,
    delegation_grant,
    note_child_run_id,
    observe_child_event,
    observe_child_record,
)
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import prepare_agent_execution_context, render_prepared_messages_text
from mindroom.history.interrupted_replay import (
    persist_interrupted_replay,
    split_interrupted_tool_trace,
    tool_execution_call_id,
)
from mindroom.history.mid_turn_compaction import without_fold_records
from mindroom.history.native import with_native_history
from mindroom.history.replay import compaction_marker
from mindroom.history.request_messages import build_request, replayed_history
from mindroom.history.runtime import note_prepared_history_timing
from mindroom.history.session_context import ScopeSessionContext, open_resolved_scope_session_context
from mindroom.history.storage import new_scope_session
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    render_enrichment_block,
    render_system_enrichment_block,
    render_transient_context,
)
from mindroom.knowledge.utils import resolve_agent_knowledge_access_async
from mindroom.llm.events import ContentDelta, RequestCompleted, RunFinished, ToolCompleted, ToolStarted
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
    BlockingAttemptResolution,
    BlockingTurnAdapter,
    CompletedAttempt,
    ExcludedAttempt,
    HandledAttempt,
    ResponsePausedForApproval,
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
    skip_unapproved_attempt,
    stream_response_turn,
    stream_with_llm_request_log_context,
)
from mindroom.session_storage.store import persist_to
from mindroom.timing import DispatchPipelineTiming, emit_timing_event, timed, timed_block, timing_scope
from mindroom.tool_jobs.notices import append_turn_start_job_note
from mindroom.tool_jobs.settings import background_tool_jobs_enabled
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.events import (
    AuditedToolExecution,
    CollectedStreamPresentation,
    StreamingToolTracker,
    complete_pending_tool_block,
    format_tool_combined,
)
from mindroom.tool_system.runtime_context import ToolRuntimeModelBinding, get_tool_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.model_loop import LoopHooks
    from mindroom.agent_loop.run import Persist
    from mindroom.agent_loop.spec import AgentSpec
    from mindroom.ai_turn_state import AITurnState
    from mindroom.approval.resume import ResumeDecisions, ResumeEvents
    from mindroom.config.main import Config, ResolvedRuntimeModel
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.history.request_messages import TurnMessages
    from mindroom.history.types import CompactionLifecycle, PreparedHistoryState, ScopeSummary
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.knowledge.search import AgentKnowledge
    from mindroom.llm.events import RunEvent
    from mindroom.llm.provider import ModelRequest
    from mindroom.llm.runs import AnyRunRecord, ToolExecution
    from mindroom.llm.usage import MessageMetrics, RunMetrics
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.response_turn import (
        EmptyRunDiscard,
        PausedAttempt,
        ResumedAttempt,
        StandaloneReplaySnapshot,
        TurnRunState,
    )
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from mindroom.tool_system.agno_toolkits import Function
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

__all__ = [
    "AIStreamChunk",
    "ResponseTurnContext",
    "agent_run_metadata_content",
    "ai_response",
    "build_matrix_run_metadata",
    "collect_streamed_response_content",
    "run_delegated_child_response",
    "stream_agent_response",
]
AIStreamChunk = str | ContentDelta | ToolStarted | ToolCompleted | RunFinished


def _compose_current_turn_prompt(
    *,
    raw_prompt: str,
    model_prompt: str | None,
) -> str:
    """Build the persisted current-turn user message without transient context."""
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


def _initial_agent_continuation(
    *,
    prompt: str,
    model_prompt: str | None,
    current_timestamp_ms: float | None,
    current_prompt_is_structured: bool,
    current_event_id: str | None,
    run_id: str | None,
) -> TurnContinuationRunState:
    """Build the initial continuation state for one agent response turn."""
    return TurnContinuationRunState.initial(
        prompt=prompt,
        model_prompt=model_prompt,
        current_timestamp_ms=current_timestamp_ms,
        current_prompt_is_structured=current_prompt_is_structured,
        current_event_id=current_event_id,
        run_id=run_id,
        continuation_model_prompt_tail=_model_prompt_tail_after_raw_prompt(
            raw_prompt=prompt,
            model_prompt=model_prompt,
        ),
    )


@dataclass(frozen=True)
class _PreparedAgentRun:
    """Prepared agent invocation state after history planning.

    ``spec`` is the build's spec with this turn's system message; ``history`` and ``summary`` are the stored
    history the request replays under the resolved plan, and ``turn`` is the new input around the prompt.
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
        turn = deepcopy(self.turn)
        prompt = turn.prompt
        prompt.audio = media.audio or None
        prompt.images = media.images or None
        prompt.files = media.files or None
        prompt.videos = media.videos or None
        return build_request(system=(), summary=self.summary, history=deepcopy(self.history), turn=turn)


@dataclass(frozen=True)
class _AgentAttempt:
    """Inputs and identity for one agent run attempt."""

    run_input: list[Message]
    run_id: str


@dataclass
class _TerminalRecord:
    """The terminal record a run handed to its persist callback, however the run ended."""

    record: AnyRunRecord | None = None


def _without_terminal_record(run_id: str) -> RuntimeError:
    """Return the error for an agent run that finished without handing over the record it ended with."""
    return RuntimeError(f"agent run {run_id!r} ended without its terminal record")


@dataclass(frozen=True)
class _AttemptStorage:
    """Where one attempt's run is stored: the scope's store and the session whose state its tools share."""

    store: ConversationStore | None
    session: ConversationSession
    scope: HistoryScope | None


_render_system_enrichment_context = timed("system_prompt_assembly.system_enrichment_render")(
    render_system_enrichment_block,
)


@dataclass
class _StreamingAttemptState:
    assistant_text: str = ""
    full_response: str = ""
    tool_count: int = 0
    observed_tool_calls: int = 0
    tool_tracker: StreamingToolTracker = field(default_factory=StreamingToolTracker)
    latest_request_usage: MessageMetrics | None = None
    terminal: _TerminalRecord = field(default_factory=_TerminalRecord)
    finished: bool = False
    completed_tool_executions: list[ToolExecution] = field(default_factory=list)
    first_token_logged: bool = False
    error: Exception | None = None

    @property
    def pending_tools(self) -> list[Any]:
        return self.tool_tracker.pending_tools

    @property
    def completed_tools(self) -> list[ToolTraceEntry]:
        return self.tool_tracker.completed_tools


@dataclass
class _AgentTurnHolder:
    """Live per-turn agent state shared between attempt closures and adapter callbacks."""

    attempt: _AgentAttempt | None = None
    state: _StreamingAttemptState | None = None  # streaming turns only
    attempt_started: bool = False  # streaming turns only


@dataclass(frozen=True)
class _AgentTurnCallbacks:
    """Adapter callbacks shared by the blocking and streaming agent turns."""

    open_scope: Callable[[], AbstractContextManager[ScopeSessionContext | None]]
    on_scope_opened: Callable[[ScopeSessionContext | None], None]
    release_attempt_entity: Callable[[ScopeSessionContext | None], None]
    discard_empty_run: Callable[[ScopeSessionContext | None, EmptyRunDiscard], None]
    persist_standalone_replay: Callable[[ScopeSessionContext | None, StandaloneReplaySnapshot], None]


def _build_agent_turn_callbacks(
    holder: _AgentTurnHolder,
    *,
    agent_name: str,
    prompt: str,
    current_prompt_is_structured: bool,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    execution_identity: ToolExecutionIdentity | None,
) -> _AgentTurnCallbacks:
    """Build the entity-specific turn-driver callbacks for one agent response."""

    def _open_scope() -> AbstractContextManager[ScopeSessionContext | None]:
        return open_resolved_scope_session_context(
            agent_name=agent_name,
            scope=HistoryScope(kind="agent", scope_id=agent_name),
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
        )

    def _on_scope_opened(scope_context: ScopeSessionContext | None) -> None:
        ai_runtime.scrub_queued_notice_session_context(
            scope_context=scope_context,
            entity_name=agent_name,
        )

    def _release_attempt_entity(_scope_context: ScopeSessionContext | None) -> None:
        # Cancel snapshots taken between this release and the next attempt must
        # not see the spent attempt's partials; its completed tools already
        # carried over via the turn state.
        holder.attempt = None
        holder.state = None

    def _discard_empty_run(scope_context: ScopeSessionContext | None, discard: EmptyRunDiscard) -> None:
        ai_runtime.discard_empty_completed_run(
            scope_context=scope_context,
            session_id=discard.session_id or session_id,
            run_id=discard.run_id,
            entity_name=agent_name,
            output_tokens=discard.output_tokens,
        )

    def _persist_standalone_replay(
        scope_context: ScopeSessionContext | None,
        snapshot: StandaloneReplaySnapshot,
    ) -> None:
        persist_interrupted_replay(
            scope_context=scope_context,
            run_id=snapshot.run_id,
            user_message=prompt,
            user_message_is_structured=current_prompt_is_structured,
            partial_text=snapshot.partial_text,
            completed_tools=snapshot.completed_tools,
            interrupted_tools=snapshot.interrupted_tools,
            run_metadata=snapshot.run_metadata,
            original_status=snapshot.original_status,
        )

    return _AgentTurnCallbacks(
        open_scope=_open_scope,
        on_scope_opened=_on_scope_opened,
        release_attempt_entity=_release_attempt_entity,
        discard_empty_run=_discard_empty_run,
        persist_standalone_replay=_persist_standalone_replay,
    )


def _streaming_partial_snapshot(holder: _AgentTurnHolder) -> TurnPartialSnapshot:
    """Return the live partial-output view of one streaming agent turn."""
    state = holder.state or _StreamingAttemptState()
    return TurnPartialSnapshot(
        assistant_text=state.assistant_text,
        completed_tools=tuple(state.completed_tools),
        interrupted_tools=tuple(pending.trace_entry for pending in state.pending_tools),
        attempt_run_id=holder.attempt.run_id if holder.attempt is not None else None,
    )


@dataclass(frozen=True)
class _AgentRunContext:
    """Prepared state shared by one top-level agent response lifecycle."""

    turn: ResponseTurnContext
    # turn.session_id narrowed to non-None once at the entry boundary; read
    # this field, not turn.session_id, wherever a str is required.
    session_id: str
    prompt: str
    model_prompt: str | None
    prepared_run: _PreparedAgentRun
    metadata: dict[str, Any] | None
    # The settings of every request of this run: the build's, with the turn's native history filled in.
    request_template: ModelRequest
    config: Config
    runtime_paths: RuntimePaths
    compaction_lifecycle: CompactionLifecycle | None
    turn_recorder: TurnRecorder | None
    # What the run's delegations need: whose turn it is, how deep it runs, and the shared knowledge refresher.
    execution_identity: ToolExecutionIdentity | None
    delegation_depth: int
    refresh_scheduler: KnowledgeRefreshScheduler | None

    @property
    def agent_name(self) -> str:
        """Return the agent name; agent turns label the context with it."""
        return self.turn.entity_label


@dataclass(frozen=True)
class _NonStreamingAttemptResult:
    """Result of running one non-streaming agent attempt sequence."""

    record: AnyRunRecord | None
    user_error: Exception | None = None


async def collect_streamed_response_content(
    response_stream: AsyncIterator[AIStreamChunk],
    *,
    presentation: CollectedStreamPresentation,
    on_update: Callable[[], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolTraceEntry]]:
    """Collect a stream into its presentation owner, retaining pending text and tool state.

    ``on_update`` runs after each chunk has been applied to ``presentation``. The stream is closed before any failure
    propagates, so its producer stops then.
    """
    try:
        async with closing_async_stream(response_stream):
            async for chunk in response_stream:
                if isinstance(chunk, str):
                    presentation.append_text(chunk)
                elif isinstance(chunk, ContentDelta):
                    presentation.append_text(chunk.text)
                elif isinstance(chunk, RunFinished):
                    if chunk.record.content is not None:
                        presentation.canonical_final_body_candidate = str(chunk.record.content)
                elif isinstance(chunk, ToolStarted):
                    presentation.start_tool(chunk.tool)
                elif isinstance(chunk, ToolCompleted):
                    presentation.complete_tool(chunk.tool)
                if on_update is not None:
                    await on_update()
    except ResponsePausedForApproval as error:
        error.capture_collected_presentation(
            response_text=presentation.final_text().rstrip(),
            tool_trace=presentation.tool_trace,
        )
        raise

    return presentation.final_text(), presentation.tool_trace


async def _collect_response_body_with_trace(
    response_stream: AsyncIterator[AIStreamChunk],
    *,
    show_tool_calls: bool,
    tool_trace_collector: list[ToolTraceEntry] | None,
) -> str:
    """Collect a stream to one body, bridging the trace to an optional collector."""
    body, tool_trace = await collect_streamed_response_content(
        response_stream,
        presentation=CollectedStreamPresentation(show_tool_calls=show_tool_calls),
    )
    if tool_trace_collector is not None:
        tool_trace_collector.extend(tool_trace)
    return body


def _extract_response_content(record: AnyRunRecord, *, show_tool_calls: bool = True) -> str:
    response_parts: list[str] = []

    # Add main content if present
    if record.content:
        response_parts.append(str(record.content))

    # Add formatted tool call sections when present (and enabled).
    if show_tool_calls and record.tools:
        tool_sections: list[str] = []
        for tool_index, tool in enumerate(record.tools, start=1):
            tool_name = tool.tool_name or "tool"
            tool_args = tool.tool_args or {}
            combined, _ = format_tool_combined(tool_name, tool_args, tool.result, tool_index=tool_index)
            tool_sections.append(combined.strip())
        if tool_sections:
            response_parts.append("\n\n".join(tool_sections))

    return "\n".join(response_parts) if response_parts else ""


def _extract_tool_trace(record: AnyRunRecord) -> list[ToolTraceEntry]:
    """Extract structured tool-trace metadata from a run record."""
    if not record.tools:
        return []

    trace: list[ToolTraceEntry] = []
    for tool in record.tools:
        tool_name = tool.tool_name or "tool"
        tool_args = {str(k): v for k, v in tool.tool_args.items()} if isinstance(tool.tool_args, dict) else {}
        _, trace_entry = format_tool_combined(tool_name, tool_args, tool.result)
        trace.append(trace_entry)
    return trace


def _attach_blocking_pause_presentation(
    paused: PausedAttempt,
    record: AnyRunRecord,
    *,
    show_tool_calls: bool,
) -> PausedAttempt:
    """Render a blocking pause once, before it crosses the approval boundary."""
    presentation = CollectedStreamPresentation(
        show_tool_calls=show_tool_calls,
        track_hidden_tools=True,
    )
    presentation.append_text(_extract_response_content(record, show_tool_calls=False))
    pending_by_id: dict[str, ToolExecution] = {}
    for tool in paused.tools:
        call_id = tool_execution_call_id(tool)
        if call_id is None or call_id in pending_by_id:
            msg = "Paused approval tool is missing its exact identity"
            raise RuntimeError(msg)
        pending_by_id[call_id] = tool

    # A delegation still waits for its child, or for its turn after a sibling's, until the driver answers it.
    answered = answered_delegation_calls(record)
    seen_ids: set[str] = set()
    for tool in (*tuple(record.tools or ()), *paused.tools):
        call_id = tool_execution_call_id(tool)
        if call_id is None:
            msg = "Blocking approval output has a tool without stable identity"
            raise RuntimeError(msg)
        if call_id in seen_ids:
            continue
        seen_ids.add(call_id)
        presentation.start_tool(tool)
        # An agent's own calls name no member.
        if call_id not in pending_by_id and (not tool.is_paused or (None, call_id) in answered):
            presentation.complete_tool(tool)

    return replace(
        paused,
        response_text=presentation.final_text(),
        tool_trace=tuple(presentation.tool_trace),
    )


def _own_assistant_text(record: AnyRunRecord | None) -> str:
    """Return the last text this run's own model requests produced (``own_answers``), never an earlier reply.

    A failed run whose model said nothing holds the error's text as its ``content``, so the partial text an
    interrupted-turn record keeps comes from the run's own answers only.
    """
    if record is None:
        return ""
    return next((part for answer in reversed(own_answers(record)) if (part := answer.strip())), "")


def _reported(usage: RunMetrics | MessageMetrics | None, key: str) -> int | None:
    """Return one usage counter of a run or a request, or None when the provider reported nothing for it."""
    if usage is None:
        return None
    value = usage.to_dict().get(key)
    return value if isinstance(value, int) else None


def agent_run_metadata_content(
    config: Config,
    record: AnyRunRecord | None,
    *,
    runtime_paths: RuntimePaths,
    model_name: str,
    run_id: str | None,
    session_id: str | None,
    status: RunStatus,
    prepared_history: PreparedHistoryState | None,
    last_request: MessageMetrics | None = None,
) -> dict[str, Any]:
    """Return the run metadata an agent reply or approval resume publishes for its run ``record``.

    ``prepared_history`` is the history the run was prepared with, None for an approval resume, which prepares none.
    A streamed reply also passes its last request's usage, whose counts describe the context. The tool calls counted
    are the ones the record holds: a run that reported a tool call hands over its record however it ends.
    """
    metrics = record.metrics if record is not None else None
    return build_ai_run_metadata_content(
        config=config,
        prices=model_prices(config, runtime_paths),
        model_name=model_name,
        run_id=run_id,
        session_id=(record.session_id if record is not None else None) or session_id,
        status=status,
        model=record.model if record is not None else None,
        model_provider=record.model_provider if record is not None else None,
        metrics=metrics,
        context_metrics=metrics,
        context_input_tokens=prepared_history.prepared_context_tokens if prepared_history is not None else None,
        context_raw_input_tokens=_reported(last_request, "input_tokens"),
        context_cache_read_tokens=_reported(last_request, "cache_read_tokens"),
        context_cache_write_tokens=_reported(last_request, "cache_write_tokens"),
        tool_count=len(record.tools or ()) if record is not None else 0,
        prepared_history=prepared_history,
    )


def _attempt_run_metadata(
    collector: dict[str, Any] | None,
    run_context: _AgentRunContext,
    record: AnyRunRecord | None,
    status: RunStatus,
    *,
    run_id: str | None,
    last_request: MessageMetrics | None = None,
) -> dict[str, Any] | None:
    """Return the run metadata one attempt publishes, or None when its caller collects none."""
    if collector is None:
        return None
    prepared = run_context.prepared_run
    return agent_run_metadata_content(
        run_context.config,
        record,
        runtime_paths=run_context.runtime_paths,
        model_name=prepared.runtime_model_name,
        run_id=run_id,
        session_id=run_context.session_id,
        status=status,
        prepared_history=prepared.prepared_history,
        last_request=last_request,
    )


def _track_stream_tool_started(
    state: _StreamingAttemptState,
    tool: ToolExecution,
    *,
    show_tool_calls: bool,
) -> bool:
    """Track started tool-call metadata for streaming output."""
    display_tool_index = state.tool_count + 1 if show_tool_calls else None
    tool_msg, trace_entry = state.tool_tracker.start(tool, tool_index=display_tool_index)
    if trace_entry is None:
        return False
    state.observed_tool_calls += 1
    if not show_tool_calls or display_tool_index is None:
        return True

    state.tool_count = display_tool_index
    if tool_msg:
        state.full_response += tool_msg
    return True


def _materialize_paused_agent_tool_events(
    state: _StreamingAttemptState,
    tools: Sequence[ToolExecution],
    *,
    show_tool_calls: bool,
) -> tuple[ToolStarted, ...]:
    """Return the tool-start events of paused calls the stream has not announced yet."""
    return tuple(
        ToolStarted(tool) for tool in tools if _track_stream_tool_started(state, tool, show_tool_calls=show_tool_calls)
    )


def _track_stream_tool_completed(
    state: _StreamingAttemptState,
    tool: ToolExecution,
    *,
    show_tool_calls: bool,
    agent_name: str,
) -> None:
    """Track completed tool-call metadata for streaming output."""
    if not isinstance(tool, AuditedToolExecution):
        state.completed_tool_executions.append(tool)
    completion = state.tool_tracker.complete(tool)
    if completion is None:
        return
    tool_name, result, pending_tool, _ = completion
    if not show_tool_calls:
        return

    if pending_tool is None or pending_tool.visible_tool_index is None:
        logger.warning(
            "Missing pending tool start in AI stream; skipping completion marker",
            tool_name=tool_name,
            agent=agent_name,
        )
        return
    state.full_response, _ = complete_pending_tool_block(
        state.full_response,
        tool_name,
        result,
        tool_index=pending_tool.visible_tool_index,
    )


def _stream_completed_without_visible_output(state: _StreamingAttemptState, record: AnyRunRecord) -> bool:
    visible_text = state.full_response.strip() or (str(record.content) if record.content else "").strip()
    return not visible_text and state.observed_tool_calls == 0


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
        prompt=run_context.prompt,
        model_prompt=run_context.model_prompt,
        full_prompt=run_context.prepared_run.prompt_text,
        metadata=run_context.metadata,
    )


def _begin_attempt(
    run: TurnRunState,
    run_context: _AgentRunContext,
    continuation: TurnContinuationRunState,
    media: MediaInputs,
) -> tuple[_AttemptStorage, _AgentAttempt]:
    """Record one prepared attempt on the turn's run state, and return where its run is stored and its request.

    A scope without a stored session gets its first session, which stays on the turn's scope context, so a later
    attempt of the same turn replays this one's run and an empty-run discard finds it, as a reload of the scope would.
    """
    prepared_run = run_context.prepared_run
    run.unseen_event_ids = prepared_run.unseen_event_ids
    run.run_metadata = run_context.metadata
    if run.scope_context is None:
        storage = _AttemptStorage(
            store=prepared_run.build.store,
            session=new_scope_session(
                session_id=run_context.session_id,
                scope_id=run_context.agent_name,
                is_team=False,
            ),
            scope=None,
        )
    else:
        scope_context = run.scope_context = run.scope_context.with_session()
        assert scope_context.session is not None
        storage = _AttemptStorage(store=scope_context.storage, session=scope_context.session, scope=scope_context.scope)
    attempt = _AgentAttempt(
        run_input=prepared_run.run_input(media),
        run_id=continuation.active_run_id or str(uuid4()),
    )
    return storage, attempt


def _persist_into(terminal: _TerminalRecord, storage: _AttemptStorage) -> Persist[RunRecord]:
    """Return the persist callback of one run: keep its terminal record and store it, which updates the loaded session."""
    store_persist = persist_to(storage.store, storage.session) if storage.store is not None else None

    async def persist(record: RunRecord) -> None:
        terminal.record = record
        if store_persist is not None:
            await store_persist(record)

    return persist


def _agent_loop_hooks(
    run_context: _AgentRunContext,
    attempt: _AgentAttempt,
    storage: _AttemptStorage,
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
            store=storage.store,
            session=storage.session,
            scope=storage.scope,
            replay_plan=prepared.prepared_history.replay_plan,
            prompt_id=prepared.turn.prompt.id,
            lifecycle=run_context.compaction_lifecycle,
            turn_recorder=run_context.turn_recorder,
        ),
        agent_name=run_context.agent_name,
        participation=ctx.participation,
        skill_review_capture=ctx.skill_review_capture,
        run_id=attempt.run_id,
    )


async def _start_agent_run(
    run_context: _AgentRunContext,
    attempt: _AgentAttempt,
    *,
    storage: _AttemptStorage,
    terminal: _TerminalRecord,
    stream: bool,
) -> AsyncIterator[RunEvent]:
    """Run one attempt on the owned loop, storing its terminal record where the turn's scope stores.

    A run that pauses on delegations is driven on: its children run, and the run continues with their results
    through ``_continue_turn_run`` until it completes or waits for an approval. An agent whose shell reaches its
    other tools through `mindroom-agent` binds this attempt's CLI first; a minimal turn then offers only Bash, and
    the hidden calls' tool events join the run's own.
    """
    prepared = run_context.prepared_run
    build = prepared.build
    run = RunContext(
        run_id=attempt.run_id,
        session_id=run_context.session_id,
        user_id=run_context.turn.requester_id,
        metadata=run_context.metadata,
        session=storage.session,
    )
    spec = prepared.spec
    hooks = _agent_loop_hooks(run_context, attempt, storage)
    if build.surface.mode == "minimal":
        spec, hooks = await minimal_attempt(
            build,
            spec,
            hooks,
            run=run,
            store=storage.store,
            response_context=run_context.turn,
            context_documents=prepared.cli_context,
            delegation_depth=run_context.delegation_depth,
            refresh_scheduler=run_context.refresh_scheduler,
        )
    elif build.surface.cli_shell:
        await bind_standard_cli(
            build,
            run=run,
            store=storage.store,
            response_context=run_context.turn,
            delegation_depth=run_context.delegation_depth,
            refresh_scheduler=run_context.refresh_scheduler,
        )
    events = run_agent(
        spec,
        attempt.run_input,
        ctx=run,
        hooks=hooks,
        persist=_persist_into(terminal, storage),
        request_template=replace(run_context.request_template, stream=stream),
    )
    driven = drive_delegation_stream(
        events,
        store=storage.store,
        session=storage.session,
        continue_parent=partial(
            _continue_turn_run,
            run_context,
            spec=spec,
            hooks=hooks,
            storage=storage,
            terminal=terminal,
        ),
        agent_name=run_context.agent_name,
        run_child=run_delegated_child_response,
        config=run_context.config,
        runtime_paths=run_context.runtime_paths,
        execution_identity=run_context.execution_identity,
        delegation_depth=run_context.delegation_depth,
        refresh_scheduler=run_context.refresh_scheduler,
    )
    if build.surface.mode == "minimal" or build.surface.cli_shell:
        driven = stream_cli_events(driven)
    async with closing_async_stream(driven):
        async for event in driven:
            yield event


async def _continue_turn_run(
    run_context: _AgentRunContext,
    paused: RunRecord,
    decisions: ResumeDecisions,
    on_event: ResumeEvents | None,
    *,
    spec: AgentSpec,
    hooks: LoopHooks,
    storage: _AttemptStorage,
    terminal: _TerminalRecord,
) -> RunRecord:
    """Continue this attempt's paused run with its delegations' results, as the spec and hooks it ran with.

    The run keeps its hooks for its whole life, so per-run hook state, such as the output-limit count or a failed
    compaction's stop, carries across its continuations. The resume replays the scope's history as the attempt did
    and reuses the paused run's stored system message.
    """
    prepared = run_context.prepared_run
    events = resume_from_session(
        spec,
        paused,
        decisions,
        session=storage.session,
        scope=storage.scope,
        settings=prepared.build.history_settings,
        plan=prepared.prepared_history.replay_plan,
        user_id=run_context.turn.requester_id,
        hooks=hooks,
        persist=_persist_into(terminal, storage),
        request_template=run_context.request_template,
    )
    return await forward_resume(events, on_event, record_type=RunRecord)


@timed("model_request_to_completion")
async def _run_cached_agent_attempt(
    run_context: _AgentRunContext,
    attempt: _AgentAttempt,
    *,
    storage: _AttemptStorage,
    terminal: _TerminalRecord,
    run_id_callback: Callable[[str], None] | None,
    pipeline_timing: DispatchPipelineTiming | None,
) -> AnyRunRecord:
    """Run one non-streaming agent attempt and return its finished record."""
    ai_runtime.note_attempt_run_id(run_id_callback, attempt.run_id)
    if pipeline_timing is not None:
        pipeline_timing.mark_model_request()
    events = _start_agent_run(run_context, attempt, storage=storage, terminal=terminal, stream=False)
    record: AnyRunRecord | None = None
    async with closing_async_stream(events):
        async for event in events:
            if isinstance(event, RunFinished):
                record = event.record
    if record is None:
        raise _without_terminal_record(attempt.run_id)
    return record


async def _run_non_streaming_agent_attempts(
    *,
    run_context: _AgentRunContext,
    attempt: _AgentAttempt,
    storage: _AttemptStorage,
    run_id_callback: Callable[[str], None] | None,
    scope_context: ScopeSessionContext | None,
    pipeline_timing: DispatchPipelineTiming | None,
) -> _NonStreamingAttemptResult:
    """Run one non-streaming agent response attempt."""
    terminal = _TerminalRecord()
    try:
        with bind_llm_request_log_context(**_attempt_request_log_context(run_context)):
            record = await _run_cached_agent_attempt(
                run_context,
                attempt,
                storage=storage,
                terminal=terminal,
                run_id_callback=run_id_callback,
                pipeline_timing=pipeline_timing,
            )
        return _NonStreamingAttemptResult(record=record)
    except Exception as e:
        logger.exception("Error generating AI response", agent=run_context.agent_name)
        return _NonStreamingAttemptResult(record=terminal.record, user_error=e)
    finally:
        ai_runtime.register_scope_notice_storage(scope_context=scope_context, entity_name=run_context.agent_name)


def _failed_agent_attempt(
    ctx: ResponseTurnContext,
    error: Exception,
    runtime_paths: RuntimePaths,
) -> BlockingAttemptResolution:
    """Keep pre-decision failures quiet; approved turns retain ordinary error replies."""
    return skip_unapproved_attempt(ctx.participation, reason="preparation_failed") or ExcludedAttempt(
        RunStatus.error,
        get_user_friendly_error_message(error, ctx.entity_label, runtime_paths=runtime_paths),
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


def _turn_system_message(build: AgentBuild, ctx: ResponseTurnContext, session_preamble: str) -> Message:
    """Render the turn's system message: the build's prompt, the memory preamble, and the turn's system enrichment."""
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
    entity = config.resolve_entity(ctx.entity_label)
    if (
        entity.memory_backend != "file"
        or not entity.history_settings.replays_whole_history
        or not (entity.has_authored_compaction_config and entity.compaction_config.enabled)
    ):
        return None
    session = scope_context.session
    compaction = compaction_marker(session, scope_context.scope) if session is not None else (None, None)
    return ConversationRef(scope_context.session_id, compaction)


async def _prepare_turn_memory(
    ctx: ResponseTurnContext,
    prompt: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    conversation: ConversationRef | None,
) -> MemoryPromptParts:
    """Recall memories for a configured agent turn; an authored persona recalls none."""
    if ctx.persona is not None:
        return MemoryPromptParts()
    return await build_memory_prompt_parts(
        prompt,
        ctx.entity_label,
        runtime_paths.storage_root,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        conversation=conversation,
    )


async def _system_message_after_compaction(
    system_message: Message,
    build: AgentBuild,
    ctx: ResponseTurnContext,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
    scope_context: ScopeSessionContext | None,
    conversation: ConversationRef | None,
) -> Message:
    """Render the system message again with the current MEMORY.md when this turn's history preparation compacted.

    That compaction already changes the request after the system prompt, so reloading here costs nothing, while
    waiting for the next turn would make it miss the cache on the whole compacted history.
    """
    compacted = _memory_conversation(ctx, config, scope_context)
    if compacted is None or compacted == conversation:
        return system_message
    fresh = await conversation_entrypoint_preamble(
        ctx.entity_label,
        runtime_paths.storage_root,
        config,
        runtime_paths,
        execution_identity,
        conversation=compacted,
    )
    return _turn_system_message(build, ctx, fresh)


@timed("system_prompt_assembly")
async def _prepare_agent_and_prompt(
    ctx: ResponseTurnContext,
    *,
    prompt: str,
    runtime_paths: RuntimePaths,
    config: Config,
    scope_context: ScopeSessionContext | None = None,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    knowledge: AgentKnowledge | None = None,
    include_interactive_questions: bool = True,
    tool_function_filter: Callable[[Function], bool] | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    include_openai_compat_guidance: bool = False,
    model_prompt: str | None = None,
    current_timestamp_ms: float | None = None,
    current_event_id: str | None = None,
    current_prompt_is_structured: bool = False,
    pipeline_timing: DispatchPipelineTiming | None = None,
    eager_deferred_tools: bool = False,
    supports_native_tool_approval: bool = False,
    reusable_build: AgentBuild | None = None,
) -> _PreparedAgentRun:
    """Prepare the agent build, this turn's system message, and its request input.

    Returns the prepared run input plus history bookkeeping for one agent turn.
    """
    agent_name = ctx.entity_label
    _assert_agent_target(agent_name, config)
    storage_path = runtime_paths.storage_root

    def _resolve_model_and_build_agent() -> tuple[ResolvedRuntimeModel, AgentBuild]:
        """Resolve the runtime model and build the agent off the event loop (#1260).

        Both walk the filesystem per dispatch: thread-model overrides, workspace
        scaffolding, context-file reads, and session storage opens.
        """
        runtime_model = config.resolve_runtime_model(
            entity_name=agent_name,
            active_model_name=ctx.active_model_name,
            room_id=ctx.room_id,
            thread_id=ctx.thread_id,
            runtime_paths=runtime_paths,
        )
        build = reusable_build
        if build is None:
            build = create_agent(
                agent_name,
                config,
                runtime_paths,
                history_storage=scope_context.storage if scope_context is not None else None,
                active_model_name=runtime_model.model_name,
                knowledge=knowledge,
                include_interactive_questions=include_interactive_questions,
                tool_function_filter=tool_function_filter,
                include_openai_compat_guidance=include_openai_compat_guidance,
                execution_identity=execution_identity,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                supports_native_tool_approval=supports_native_tool_approval,
                eager_deferred_tools=eager_deferred_tools,
                agent_mode=ctx.agent_mode,
                agent_cli_in_shell=True,
                persona=ctx.persona,
            )
        return runtime_model, build

    memory_backend = config.resolve_entity(agent_name).memory_backend
    memory_conversation = _memory_conversation(ctx, config, scope_context)
    parallel_branches = memory_backend == "mem0"
    serial_reason: str | None = None
    if memory_backend == "file":
        parallel_branches = reusable_build is not None or await asyncio.to_thread(
            agent_build_can_overlap_file_memory,
            agent_name,
            config,
            storage_path,
        )
        if not parallel_branches:
            serial_reason = "default_workspace_scaffold_pending"
    if pipeline_timing is not None:
        pipeline_timing.note(
            prompt_branches_parallel=parallel_branches,
            prompt_branches_memory_backend=memory_backend,
            prompt_branches_serial_reason=serial_reason,
        )
    current_turn_prompt = _compose_current_turn_prompt(raw_prompt=prompt, model_prompt=model_prompt)
    if parallel_branches:
        _mark_pipeline_timing(pipeline_timing, "prompt_branches_start")
        with timed_block(
            "system_prompt_assembly.memory_agent_join",
            parallel=True,
            memory_backend=memory_backend,
        ):
            try:
                prompt_parts, runtime_model, build = await prepare_prompt_branches(
                    prepare_memory=partial(
                        _prepare_turn_memory,
                        ctx,
                        prompt,
                        config,
                        runtime_paths,
                        execution_identity,
                        memory_conversation,
                    ),
                    build_agent=_resolve_model_and_build_agent,
                    agent_name=agent_name,
                    pipeline_timing=pipeline_timing,
                )
            finally:
                _mark_pipeline_timing(pipeline_timing, "prompt_branches_ready")
    else:
        _mark_pipeline_timing(pipeline_timing, "memory_prepare_start")
        prompt_parts = await _prepare_turn_memory(
            ctx,
            prompt,
            config,
            runtime_paths,
            execution_identity,
            memory_conversation,
        )
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
        system_message, transient_turn_context, cli_context = (
            minimal.system_message,
            minimal.transient_context,
            minimal.context_documents,
        )
    else:
        system_message = _turn_system_message(build, ctx, prompt_parts.session_preamble)
        transient_turn_context = render_transient_context(
            (prompt_parts.transient_turn_context, render_enrichment_block(list(ctx.transient_enrichment_items))),
        )
    prepared_execution = await prepare_agent_execution_context(
        ctx,
        scope_context=scope_context,
        # A minimal turn's requests offer only Bash, so its history budget is sized with Bash alone.
        build=presented_build(build) if build.surface.mode == "minimal" else build,
        system_message=system_message,
        prompt=current_turn_prompt,
        transient_context_messages=(
            (Message(role="user", content=transient_turn_context, add_to_agent_memory=False),)
            if transient_turn_context
            else ()
        ),
        thread_history=thread_history,
        runtime_paths=runtime_paths,
        config=config,
        resolved_runtime_model=runtime_model,
        compaction_lifecycle=compaction_lifecycle,
        current_sender_id=None if include_openai_compat_guidance else ctx.current_sender_id or ctx.requester_id,
        current_timestamp_ms=current_timestamp_ms,
        current_event_id=current_event_id,
        current_prompt_is_structured=current_prompt_is_structured,
        include_openai_compat_guidance=include_openai_compat_guidance,
        pipeline_timing=pipeline_timing,
    )
    system_message = await _system_message_after_compaction(
        system_message,
        build,
        ctx,
        config,
        runtime_paths,
        execution_identity,
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
        spec=replace(build.spec, system_message=system_message),
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
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    knowledge: AgentKnowledge | None,
    include_interactive_questions: bool,
    tool_function_filter: Callable[[Function], bool] | None,
    include_openai_compat_guidance: bool,
    execution_identity: ToolExecutionIdentity | None,
    compaction_lifecycle: CompactionLifecycle | None,
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    turn_recorder: TurnRecorder | None,
    pipeline_timing: DispatchPipelineTiming | None,
    eager_deferred_tools: bool = False,
    supports_native_tool_approval: bool = False,
    reusable_build: AgentBuild | None = None,
) -> _AgentRunContext:
    """Prepare one attempt of an agent response, as its ``continuation`` state asks, through metadata assembly."""
    if continuation.active_model_name is not None:
        ctx = replace(ctx, active_model_name=continuation.active_model_name)
    prompt = continuation.active_prompt
    model_prompt = continuation.active_model_prompt
    if pipeline_timing is not None:
        pipeline_timing.mark("ai_prepare_start", overwrite=True)
    if delegation_depth == 0 and ctx.agent_mode != "minimal":
        # Only a top-level standard reply can collect jobs, so only it hears which ended.
        ctx = replace(ctx, transient_enrichment_items=await append_turn_start_job_note(ctx.transient_enrichment_items))
    prepared_run = await _prepare_agent_and_prompt(
        ctx,
        prompt=prompt,
        runtime_paths=runtime_paths,
        config=config,
        scope_context=scope_context,
        thread_history=thread_history,
        knowledge=knowledge,
        include_interactive_questions=include_interactive_questions,
        tool_function_filter=tool_function_filter,
        execution_identity=execution_identity,
        compaction_lifecycle=compaction_lifecycle,
        delegation_depth=delegation_depth,
        refresh_scheduler=refresh_scheduler,
        include_openai_compat_guidance=include_openai_compat_guidance,
        model_prompt=model_prompt,
        current_timestamp_ms=continuation.active_current_timestamp_ms,
        current_event_id=continuation.active_current_event_id,
        current_prompt_is_structured=continuation.active_current_prompt_is_structured,
        pipeline_timing=pipeline_timing,
        eager_deferred_tools=eager_deferred_tools,
        supports_native_tool_approval=supports_native_tool_approval,
        reusable_build=reusable_build,
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
        model_params=build.spec.model.model_params,
        extra_metadata=deep_merge_metadata(without_fold_records(ctx.matrix_run_metadata), run_extra_content),
    )
    if ctx.agent_mode == "minimal":
        metadata = dict(metadata or {}) | {"agent_mode": "minimal"}
    if turn_recorder is not None:
        turn_recorder.set_run_metadata(metadata)

    return _AgentRunContext(
        turn=ctx,
        session_id=session_id,
        prompt=prompt,
        model_prompt=model_prompt,
        prepared_run=prepared_run,
        metadata=metadata,
        request_template=request_template,
        config=config,
        runtime_paths=runtime_paths,
        compaction_lifecycle=compaction_lifecycle,
        turn_recorder=turn_recorder,
        execution_identity=execution_identity,
        delegation_depth=delegation_depth,
        refresh_scheduler=refresh_scheduler,
    )


async def run_delegated_child_response(
    child: DelegationChild,
    *,
    prompt: str,
    config: Config,
    runtime_paths: RuntimePaths,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    supports_native_tool_approval: bool,
    approval_config: Config | None = None,
) -> str:
    """Execute the normal response envelope for a prepared child owned by either adapter.

    A minimal child cannot pause for approval: approval-gated tools stay hidden from it,
    so no adapter ever has to resume it natively.
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
    )
    # The child's tools run as the child, whichever adapter runs it; its caller's identity stays with the caller.
    with tool_runtime_context(child_context):
        return await run_with_tool_execution_identity(
            identity,
            operation=lambda: ai_response(
                turn,
                prompt=prompt,
                runtime_paths=runtime_paths,
                config=active_config,
                knowledge=knowledge.knowledge,
                run_id_callback=lambda run_id: note_child_run_id(child, run_id, runtime_paths),
                include_interactive_questions=False,
                include_openai_compat_guidance=identity.channel == "openai_compat",
                tool_function_filter=context.tool_function_filter if context is not None else None,
                execution_identity=identity,
                delegation_depth=child.depth,
                refresh_scheduler=refresh_scheduler,
                attempt_model_runtime=ToolRuntimeModelBinding(),
                supports_native_tool_approval=supports_native_tool_approval and child.agent_mode == "standard",
                collect_streamed_response=True,
                show_tool_calls=False,
                turn_recorder=TurnRecorder(user_message=prompt),
            ),
        )


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


async def ai_response(  # noqa: C901
    ctx: ResponseTurnContext,
    prompt: str,
    runtime_paths: RuntimePaths,
    config: Config,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_prompt: str | None = None,
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    knowledge: AgentKnowledge | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    include_interactive_questions: bool = True,
    tool_function_filter: Callable[[Function], bool] | None = None,
    include_openai_compat_guidance: bool = False,
    media: MediaInputs | None = None,
    show_tool_calls: bool = True,
    collect_streamed_response: bool = False,
    tool_trace_collector: list[ToolTraceEntry] | None = None,
    run_metadata_collector: dict[str, Any] | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    turn_recorder: TurnRecorder | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    eager_deferred_tools: bool = False,
    supports_native_tool_approval: bool = False,
    attempt_model_runtime: ai_runtime.AttemptModelRuntime | None = None,
    reusable_build: AgentBuild | None = None,
) -> str:
    """Generate one agent response with memory integration and return its text.

    Takes the arguments ``stream_agent_response`` documents, without its resume inputs and completion sink, plus
    ``collect_streamed_response``, which runs the turn as a stream and collects it into one body (as a minimal-mode
    turn always does), and ``tool_trace_collector``, an optional list that receives the run's structured tool-trace
    entries.
    """
    ctx = _with_background_tool_jobs(ctx, config, runtime_paths)
    agent_name = ctx.entity_label
    logger.info("AI request", agent=agent_name, room_id=ctx.room_id)
    if collect_streamed_response or ctx.agent_mode == "minimal":
        return await _collect_response_body_with_trace(
            stream_agent_response(
                ctx,
                prompt=prompt,
                runtime_paths=runtime_paths,
                config=config,
                thread_history=thread_history,
                model_prompt=model_prompt,
                current_timestamp_ms=current_timestamp_ms,
                current_prompt_is_structured=current_prompt_is_structured,
                knowledge=knowledge,
                run_id_callback=run_id_callback,
                include_interactive_questions=include_interactive_questions,
                tool_function_filter=tool_function_filter,
                include_openai_compat_guidance=include_openai_compat_guidance,
                media=media,
                show_tool_calls=show_tool_calls,
                run_metadata_collector=run_metadata_collector,
                execution_identity=execution_identity,
                compaction_lifecycle=compaction_lifecycle,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                turn_recorder=turn_recorder,
                pipeline_timing=pipeline_timing,
                eager_deferred_tools=eager_deferred_tools,
                supports_native_tool_approval=supports_native_tool_approval,
                attempt_model_runtime=attempt_model_runtime,
                reusable_build=reusable_build,
            ),
            show_tool_calls=show_tool_calls,
            tool_trace_collector=tool_trace_collector,
        )

    session_id = _turn_session_id(ctx)
    media_inputs = media or MediaInputs()
    try:
        _assert_agent_target(agent_name, config)
    except ValueError as e:
        return get_user_friendly_error_message(e, agent_name, runtime_paths=runtime_paths)

    holder = _AgentTurnHolder()
    callbacks = _build_agent_turn_callbacks(
        holder,
        agent_name=agent_name,
        prompt=_compose_current_turn_prompt(raw_prompt=prompt, model_prompt=model_prompt),
        current_prompt_is_structured=current_prompt_is_structured,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
    )

    async def _run_blocking_attempt(
        run: TurnRunState,
        continuation_state: TurnContinuationRunState,
    ) -> BlockingAttemptResolution:
        """Run one prepared agent attempt."""
        try:
            run_context = await _prepare_agent_run_context(
                ctx,
                continuation_state,
                session_id=session_id,
                runtime_paths=runtime_paths,
                config=config,
                scope_context=run.scope_context,
                thread_history=thread_history,
                knowledge=knowledge,
                include_interactive_questions=include_interactive_questions,
                tool_function_filter=tool_function_filter,
                include_openai_compat_guidance=include_openai_compat_guidance,
                execution_identity=execution_identity,
                compaction_lifecycle=compaction_lifecycle,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                turn_recorder=turn_recorder,
                pipeline_timing=pipeline_timing,
                eager_deferred_tools=eager_deferred_tools,
                supports_native_tool_approval=supports_native_tool_approval,
                reusable_build=reusable_build,
            )
        except Exception as e:
            logger.exception("Error preparing agent", agent=agent_name)
            return _failed_agent_attempt(ctx, e, runtime_paths)
        prepared_run = run_context.prepared_run
        storage, attempt = _begin_attempt(run, run_context, continuation_state, media_inputs)
        holder.attempt = attempt
        attempt_result = await ai_runtime.run_attempt_with_model(
            attempt_model_runtime,
            active_model_name=prepared_run.runtime_model_name,
            operation=lambda: _run_non_streaming_agent_attempts(
                run_context=run_context,
                attempt=attempt,
                storage=storage,
                run_id_callback=run_id_callback,
                scope_context=run.scope_context,
                pipeline_timing=pipeline_timing,
            ),
        )
        record = attempt_result.record
        if attempt_result.user_error is not None:
            error = attempt_result.user_error
            if skipped := skip_unapproved_attempt(
                ctx.participation,
                reason="run_failed_before_decision",
                session_id=session_id,
                run_id=attempt.run_id,
            ):
                return skipped
            completed_tools, interrupted_tools = split_interrupted_tool_trace(
                record.tools if record is not None else None,
            )
            return ExcludedAttempt(
                original_status=RunStatus.error,
                response_text=get_user_friendly_error_message(error, agent_name, runtime_paths=runtime_paths),
                reason=str(error),
                partial_text=_own_assistant_text(record),
                completed_tools=tuple(completed_tools),
                interrupted_tools=tuple(interrupted_tools),
                run_id=attempt.run_id,
                metadata_content=_attempt_run_metadata(
                    run_metadata_collector,
                    run_context,
                    record,
                    RunStatus.error,
                    run_id=attempt.run_id,
                ),
            )
        if record is None:
            raise _without_terminal_record(attempt.run_id)
        if record.status is RunStatus.completed and (
            skipped := skip_unapproved_attempt(
                ctx.participation,
                reason="participation_declined",
                session_id=record.session_id or session_id,
                run_id=record.run_id,
                output_tokens=_reported(record.metrics, "output_tokens"),
            )
        ):
            return skipped

        response_tool_trace = _extract_tool_trace(record)
        if tool_trace_collector is not None:
            tool_trace_collector.extend(response_tool_trace)
        metadata_content = _attempt_run_metadata(
            run_metadata_collector,
            run_context,
            record,
            record.status,
            run_id=record.run_id,
        )

        if record.status is RunStatus.paused:
            paused_attempt = paused_attempt_from_response(
                record,
                fallback_session_id=session_id,
                fallback_run_id=attempt.run_id,
                toolkit_owners=tool_owners_by_agent(prepared_run.build),
            )
            if paused_attempt is None:
                raise pause_without_approval_error(record, entity_label="agent")
            return replace(
                _attach_blocking_pause_presentation(
                    paused_attempt,
                    record,
                    show_tool_calls=show_tool_calls,
                ),
                runtime_model_name=prepared_run.runtime_model_name,
            )
        return CompletedAttempt(
            status=record.status,
            response_text=_extract_response_content(record, show_tool_calls=show_tool_calls),
            replayable_text=_extract_response_content(record, show_tool_calls=False),
            has_visible_content=bool(record.content),
            is_empty=ai_runtime.is_empty_completed_run(record),
            session_id=record.session_id,
            run_id=record.run_id,
            attempt_run_id=attempt.run_id,
            runtime_model_name=prepared_run.runtime_model_name,
            output_tokens=_reported(record.metrics, "output_tokens"),
            tool_executions=tuple(record.tools or ()),
            control_executions=cli_control_executions(attempt.run_id),
            completed_tools=tuple(response_tool_trace),
            metadata_content=metadata_content,
        )

    adapter = BlockingTurnAdapter(
        open_scope=callbacks.open_scope,
        run_attempt=_run_blocking_attempt,
        snapshot_partial=lambda: TurnPartialSnapshot(
            attempt_run_id=holder.attempt.run_id if holder.attempt is not None else None,
        ),
        release_attempt_entity=callbacks.release_attempt_entity,
        discard_empty_run=callbacks.discard_empty_run,
        on_scope_opened=callbacks.on_scope_opened,
        persist_standalone_replay=callbacks.persist_standalone_replay,
    )
    return await run_blocking_response_turn(
        ctx,
        adapter,
        TurnSinks(turn_recorder=turn_recorder, run_metadata_collector=run_metadata_collector),
        continuation=_initial_agent_continuation(
            prompt=prompt,
            model_prompt=model_prompt,
            current_timestamp_ms=current_timestamp_ms,
            current_prompt_is_structured=current_prompt_is_structured,
            current_event_id=ctx.reply_to_event_id,
            run_id=ctx.run_id,
        ),
    )


@timed("model_request_to_completion")
async def _process_stream_events(  # noqa: C901, PLR0912
    events: AsyncIterator[RunEvent],
    *,
    state: _StreamingAttemptState,
    show_tool_calls: bool,
    agent_name: str,
    state_updated: Callable[[], None] | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> AsyncGenerator[AIStreamChunk, None]:
    """Consume one streaming attempt, yielding chunks and mutating *state*."""
    try:
        async for event in events:
            await observe_child_event(event)
            if isinstance(event, ContentDelta):
                if not event.text:
                    continue
                if not state.first_token_logged:
                    state.first_token_logged = True
                    if pipeline_timing is not None:
                        pipeline_timing.mark("model_first_token")
                state.assistant_text += event.text
                state.full_response += event.text
                if state_updated is not None:
                    state_updated()
                yield event
                continue

            if isinstance(event, ToolStarted):
                emit_timing_event(
                    "Dispatch tool-call timing",
                    phase="tool_call_started",
                    agent_name=agent_name,
                    tool_name=event.tool.tool_name,
                    tool_call_id=tool_execution_call_id(event.tool),
                    show_tool_calls=show_tool_calls,
                )
                _track_stream_tool_started(state, event.tool, show_tool_calls=show_tool_calls)
                if state_updated is not None:
                    state_updated()
                yield event
                continue

            if isinstance(event, ToolCompleted):
                emit_timing_event(
                    "Dispatch tool-call timing",
                    phase="tool_call_completed",
                    agent_name=agent_name,
                    tool_name=event.tool.tool_name,
                    tool_call_id=tool_execution_call_id(event.tool),
                    show_tool_calls=show_tool_calls,
                )
                _track_stream_tool_completed(state, event.tool, show_tool_calls=show_tool_calls, agent_name=agent_name)
                if state_updated is not None:
                    state_updated()
                yield event
                continue

            if isinstance(event, RequestCompleted):
                state.latest_request_usage = event.usage
                continue

            if isinstance(event, RunFinished):
                state.finished = True
                state.terminal.record = event.record
                if state_updated is not None:
                    state_updated()
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
    storage: _AttemptStorage,
    state: _StreamingAttemptState,
    show_tool_calls: bool,
    run_id_callback: Callable[[str], None] | None,
    state_updated: Callable[[], None] | None,
    pipeline_timing: DispatchPipelineTiming | None,
) -> AsyncGenerator[AIStreamChunk, None]:
    """Start and consume one streaming agent attempt."""
    try:
        ai_runtime.note_attempt_run_id(run_id_callback, attempt.run_id)
        request_context = _attempt_request_log_context(run_context)
        with bind_llm_request_log_context(**request_context):
            if pipeline_timing is not None:
                pipeline_timing.mark_model_request()
            run_events = _start_agent_run(run_context, attempt, storage=storage, terminal=state.terminal, stream=True)
        events = stream_with_llm_request_log_context(run_events, request_context=request_context)
        chunks = _process_stream_events(
            events,
            state=state,
            show_tool_calls=show_tool_calls,
            agent_name=run_context.agent_name,
            state_updated=state_updated,
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
    prompt: str,
    runtime_paths: RuntimePaths,
    config: Config,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_prompt: str | None = None,
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    knowledge: AgentKnowledge | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    include_interactive_questions: bool = True,
    tool_function_filter: Callable[[Function], bool] | None = None,
    include_openai_compat_guidance: bool = False,
    media: MediaInputs | None = None,
    show_tool_calls: bool = True,
    run_metadata_collector: dict[str, Any] | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    turn_recorder: TurnRecorder | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
    eager_deferred_tools: bool = False,
    supports_native_tool_approval: bool = False,
    attempt_model_runtime: ai_runtime.AttemptModelRuntime | None = None,
    reusable_build: AgentBuild | None = None,
    resumed_attempt: ResumedAttempt | None = None,
    initial_continuation_count: int = 0,
    on_completed: Callable[[CompletedAttempt], None] | None = None,
) -> AsyncIterator[AIStreamChunk]:
    """Generate one streaming agent response on the owned loop.

    Args:
        ctx: Per-turn identity constants for this response turn. ``entity_label``
            names the agent, ``session_id`` is required, and ``requester_id`` is
            the Matrix sender used for prompt attribution.
        prompt: User prompt
        runtime_paths: Runtime config/storage paths for agent data and config-aware tools
        config: Application configuration
        thread_history: Optional thread history
        model_prompt: Optional model-facing current-turn prompt additions.
        current_timestamp_ms: Optional source Matrix event timestamp in milliseconds.
        current_prompt_is_structured: Whether the current prompt already carries trusted message tags.
        knowledge: Optional shared knowledge base for RAG-enabled agents
        run_id_callback: Optional callback that receives the active run_id
            before each real streaming attempt starts.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        tool_function_filter: Optional policy applied to each resolved agent tool function.
        include_openai_compat_guidance: Whether to omit Matrix-style sender
            attribution for OpenAI-compatible prompt formatting.
        media: Optional multimodal inputs (audio/images/files/videos)
        show_tool_calls: Whether to include tool call details inline in the streamed response.
        run_metadata_collector: Optional mapping that receives versioned
            run/model/token metadata for Matrix message content.
        execution_identity: Request execution identity used to resolve scoped
            agent state, sessions, and memory consistently for this run.
        compaction_lifecycle: Optional lifecycle sink for ordered foreground
            compaction notices.
        delegation_depth: Current nested delegation depth for delegated-agent runs.
        refresh_scheduler: Optional runtime-owned shared knowledge refresh scheduler
            passed through to delegated child agents.
        turn_recorder: Optional lifecycle-owned recorder updated with trusted turn state.
        pipeline_timing: Optional dispatch timing collector updated with AI-stage milestones.
        eager_deferred_tools: Whether to send deferred toolkits as ordinary tools instead of through `call_tool`;
            models with native tool search keep native deferral.
        supports_native_tool_approval: Whether this caller can resume confirmation pauses.
        attempt_model_runtime: Optional composition-root boundary that exposes each attempt's model to tools.
        reusable_build: Optional caller-owned agent build materialized for repeated sequential turns.
        resumed_attempt: Restored attempt to settle before preparing a fresh model request.
        initial_continuation_count: Same-turn attempts consumed before the first fresh request.
        on_completed: Optional sink for the terminal typed attempt, independent of display metadata.

    Yields:
        Streaming chunks/events as they become available

    """
    ctx = _with_background_tool_jobs(ctx, config, runtime_paths)
    agent_name = ctx.entity_label
    logger.info("AI streaming request", agent=agent_name, room_id=ctx.room_id)
    session_id = _turn_session_id(ctx)
    media_inputs = media or MediaInputs()
    try:
        _assert_agent_target(agent_name, config)
    except ValueError as e:
        yield get_user_friendly_error_message(e, agent_name, runtime_paths=runtime_paths)
        return

    holder = _AgentTurnHolder()
    callbacks = _build_agent_turn_callbacks(
        holder,
        agent_name=agent_name,
        prompt=_compose_current_turn_prompt(raw_prompt=prompt, model_prompt=model_prompt),
        current_prompt_is_structured=current_prompt_is_structured,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
    )

    async def _finalize_streaming_attempt(scope_context: ScopeSessionContext | None) -> None:
        if not holder.attempt_started:
            return
        holder.attempt_started = False
        ai_runtime.register_scope_notice_storage(scope_context=scope_context, entity_name=agent_name)

    async def _run_streaming_attempt(  # noqa: C901
        run: TurnRunState,
        continuation_state: TurnContinuationRunState,
    ) -> AsyncGenerator[AIStreamChunk | AttemptResolved, None]:
        """Stream one agent attempt, ending with its ``AttemptResolved`` sentinel."""
        try:
            run_context = await _prepare_agent_run_context(
                ctx,
                continuation_state,
                session_id=session_id,
                runtime_paths=runtime_paths,
                config=config,
                scope_context=run.scope_context,
                thread_history=thread_history,
                knowledge=knowledge,
                include_interactive_questions=include_interactive_questions,
                tool_function_filter=tool_function_filter,
                include_openai_compat_guidance=include_openai_compat_guidance,
                execution_identity=execution_identity,
                compaction_lifecycle=compaction_lifecycle,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                turn_recorder=turn_recorder,
                pipeline_timing=pipeline_timing,
                eager_deferred_tools=eager_deferred_tools,
                supports_native_tool_approval=supports_native_tool_approval,
                reusable_build=reusable_build,
            )
        except Exception as e:
            logger.exception("Error preparing agent for streaming", agent=agent_name)
            if skipped := skip_unapproved_attempt(ctx.participation, reason="preparation_failed"):
                yield AttemptResolved(skipped)
                return
            yield get_user_friendly_error_message(e, agent_name, runtime_paths=runtime_paths)
            yield AttemptResolved(HandledAttempt())
            return
        prepared_run = run_context.prepared_run
        storage, attempt = _begin_attempt(run, run_context, continuation_state, media_inputs)
        holder.attempt = attempt
        holder.attempt_started = True
        turn_state = run.turn_state
        state = _StreamingAttemptState()
        holder.state = state

        def _sync_live_turn_recorder(
            *,
            state_ref: _StreamingAttemptState = state,
            turn_state_ref: AITurnState = turn_state,
            metadata_ref: dict[str, Any] | None = run.run_metadata,
        ) -> None:
            turn_state_ref.sync_partial(
                turn_recorder,
                run_metadata=metadata_ref,
                assistant_text=state_ref.assistant_text,
                completed_tools=state_ref.completed_tools,
                interrupted_tools=[pending.trace_entry for pending in state_ref.pending_tools],
            )

        attempt_stream = ai_runtime.stream_attempt_with_model(
            attempt_model_runtime,
            _stream_agent_attempt_chunks(
                run_context=run_context,
                attempt=attempt,
                storage=storage,
                state=state,
                show_tool_calls=show_tool_calls,
                run_id_callback=run_id_callback,
                state_updated=_sync_live_turn_recorder,
                pipeline_timing=pipeline_timing,
            ),
            active_model_name=prepared_run.runtime_model_name,
        )
        async with closing_async_stream(attempt_stream):
            async for stream_chunk in attempt_stream:
                yield stream_chunk

        record = state.terminal.record
        if state.error is not None:
            if isinstance(record, RunRecord):
                # A failed run stored its record and raised; a delegated child's observer learns the failure from it.
                observe_child_record(record)
            if skipped := skip_unapproved_attempt(
                ctx.participation,
                reason="run_failed_before_decision",
                session_id=session_id,
                run_id=attempt.run_id,
            ):
                yield AttemptResolved(skipped)
            else:
                yield get_user_friendly_error_message(state.error, agent_name, runtime_paths=runtime_paths)
                yield AttemptResolved(
                    HandledAttempt(
                        metadata_content=_attempt_run_metadata(
                            run_metadata_collector,
                            run_context,
                            record,
                            RunStatus.error,
                            run_id=attempt.run_id,
                            last_request=state.latest_request_usage,
                        ),
                    ),
                )
            return
        if record is None or not state.finished:
            raise _without_terminal_record(attempt.run_id)

        if skipped := skip_unapproved_attempt(
            ctx.participation,
            reason="participation_declined",
            session_id=session_id,
            run_id=attempt.run_id,
            output_tokens=_reported(record.metrics, "output_tokens"),
        ):
            yield AttemptResolved(skipped)
            return

        if record.status is RunStatus.paused:
            paused_attempt = paused_attempt_from_response(
                record,
                fallback_session_id=session_id,
                fallback_run_id=attempt.run_id,
                toolkit_owners=tool_owners_by_agent(prepared_run.build),
            )
            if paused_attempt is None:
                raise pause_without_approval_error(record, entity_label="agent")
            for tool_event in _materialize_paused_agent_tool_events(
                state,
                paused_attempt.tools,
                show_tool_calls=show_tool_calls,
            ):
                _sync_live_turn_recorder()
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
        final_text = str(record.content) if record.content is not None else ""
        yield AttemptResolved(
            CompletedAttempt(
                status=final_status,
                replayable_text=state.assistant_text or final_text,
                has_visible_content=bool(state.assistant_text or final_text),
                is_empty=completed_without_output and not state.completed_tool_executions,
                session_id=record.session_id,
                run_id=record.run_id,
                attempt_run_id=attempt.run_id,
                runtime_model_name=prepared_run.runtime_model_name,
                output_tokens=_reported(record.metrics, "output_tokens"),
                tool_executions=tuple(state.completed_tool_executions),
                control_executions=cli_control_executions(attempt.run_id),
                completed_tools=tuple(state.completed_tools),
                metadata_content=_attempt_run_metadata(
                    run_metadata_collector,
                    run_context,
                    record,
                    final_status,
                    run_id=record.run_id,
                    last_request=state.latest_request_usage,
                ),
            ),
        )

    adapter = StreamingTurnAdapter[AIStreamChunk](
        open_scope=callbacks.open_scope,
        run_attempt=_run_streaming_attempt,
        snapshot_partial=lambda: _streaming_partial_snapshot(holder),
        release_attempt_entity=callbacks.release_attempt_entity,
        discard_empty_run=callbacks.discard_empty_run,
        on_scope_opened=callbacks.on_scope_opened,
        finalize_attempt=_finalize_streaming_attempt,
        make_text_chunk=ContentDelta,
        persist_standalone_replay=callbacks.persist_standalone_replay,
    )
    response_stream = stream_response_turn(
        ctx,
        adapter,
        TurnSinks(
            turn_recorder=turn_recorder,
            run_metadata_collector=run_metadata_collector,
            on_completed=on_completed,
        ),
        resumed_attempt=resumed_attempt,
        initial_continuation_count=initial_continuation_count,
        continuation=_initial_agent_continuation(
            prompt=prompt,
            model_prompt=model_prompt,
            current_timestamp_ms=current_timestamp_ms,
            current_prompt_is_structured=current_prompt_is_structured,
            current_event_id=ctx.reply_to_event_id,
            run_id=ctx.run_id,
        ),
    )
    # Close the driver generator deterministically when this wrapper unwinds so
    # its cleanup does not wait for event-loop async-generator finalization.
    async with aclosing(response_stream) as closing_stream:
        async for chunk in closing_stream:
            yield chunk
