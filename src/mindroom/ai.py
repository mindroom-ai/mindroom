"""AI integration module for MindRoom agents and memory management."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, NoReturn, cast
from uuid import uuid4

from agno.db.base import SessionType
from agno.models.message import Message
from agno.models.metrics import Metrics
from agno.run.agent import (
    ModelRequestCompletedEvent,
    RunCancelledEvent,
    RunCompletedEvent,
    RunContentEvent,
    RunErrorEvent,
    RunOutput,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)
from agno.run.base import RunStatus

from mindroom import ai_runtime
from mindroom.agents import create_agent
from mindroom.ai_run_metadata import (
    build_ai_run_metadata_content,
    build_model_request_metrics_fallback,
    build_prepared_history_metadata_content,
    empty_request_metric_totals,
)
from mindroom.ai_turn_state import AITurnState
from mindroom.cancellation import build_cancelled_error
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
    RuntimePaths,
)
from mindroom.dynamic_tool_continuation import DYNAMIC_TOOL_CONTINUATION_LIMIT, continuation_decision_from_tools
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import prepare_agent_execution_context, render_prepared_messages_text
from mindroom.history import (
    CompactionOutcome,
    HistoryScope,
    PreparedHistoryState,
    ScopeSessionContext,
    agent_tool_definition_payloads_for_logging,
    apply_replay_plan,
    close_agent_runtime_state_dbs,
    compute_prompt_token_breakdown,
    note_prepared_history_timing,
    open_resolved_scope_session_context,
)
from mindroom.history.interrupted_replay import (
    persist_interrupted_replay,
    split_interrupted_tool_trace,
    tool_execution_call_id,
)
from mindroom.hooks import EnrichmentItem, render_system_enrichment_block
from mindroom.llm_request_logging import (
    bind_llm_request_log_context,
    build_llm_request_log_context,
    model_params_payload,
    stream_with_llm_request_log_context,
)
from mindroom.logging_config import get_logger
from mindroom.media_fallback import (
    ModelMediaRoute,
    build_model_media_route,
    filter_media_inputs_for_route,
    retry_media_inputs_after_failure,
    unsupported_media_kinds_for_route,
)
from mindroom.media_inputs import MediaInputs, MediaKind
from mindroom.memory import MemoryPromptParts, build_memory_prompt_parts, strip_user_turn_time_prefix
from mindroom.metadata_merge import deep_merge_metadata
from mindroom.timing import DispatchPipelineTiming, emit_timing_event, timed, timing_scope
from mindroom.tool_system.events import StreamingToolTracker, complete_pending_tool_block, format_tool_combined

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Collection, Sequence

    from agno.agent import Agent
    from agno.knowledge.knowledge import Knowledge
    from agno.models.base import Model
    from agno.models.response import ToolExecution

    from mindroom.config.main import Config, ResolvedRuntimeModel
    from mindroom.history import CompactionLifecycle
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

__all__ = [
    "AIStreamChunk",
    "ai_response",
    "build_matrix_run_metadata",
    "resolve_run_correlation_id",
    "stream_agent_response",
]
AIStreamChunk = str | RunContentEvent | RunCompletedEvent | ToolCallStartedEvent | ToolCallCompletedEvent


def _append_additional_context(agent: Agent, context_chunk: str) -> None:
    """Append one transient context block without discarding existing system context."""
    if not context_chunk:
        return
    existing_context = agent.additional_context.strip() if agent.additional_context else ""
    agent.additional_context = f"{existing_context}\n\n{context_chunk}" if existing_context else context_chunk


def _compose_current_turn_prompt(
    *,
    raw_prompt: str,
    model_prompt: str | None,
    prompt_parts: MemoryPromptParts,
) -> str:
    """Build the current-turn user message without rewriting persisted history."""
    prompt_chunks: list[str] = []
    if raw_prompt:
        prompt_chunks.append(raw_prompt)
    if prompt_parts.turn_context:
        prompt_chunks.append(prompt_parts.turn_context)
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


@dataclass(frozen=True)
class _DynamicContinuationRunState:
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
    ) -> _DynamicContinuationRunState:
        return cls(
            original_prompt=prompt,
            active_prompt=prompt,
            active_model_prompt=model_prompt,
            active_current_timestamp_ms=current_timestamp_ms,
            active_current_prompt_is_structured=current_prompt_is_structured,
            active_run_id=run_id,
            continuation_model_prompt_tail=_model_prompt_tail_after_raw_prompt(
                raw_prompt=prompt,
                model_prompt=model_prompt,
            ),
        )

    def advance(
        self,
        *,
        continuation_prompt: str,
        previous_run_id: str | None,
    ) -> _DynamicContinuationRunState:
        return replace(
            self,
            active_prompt=continuation_prompt,
            active_model_prompt=self.continuation_model_prompt_tail or None,
            active_current_timestamp_ms=None,
            active_current_prompt_is_structured=False,
            active_run_id=ai_runtime.next_retry_run_id(previous_run_id),
        )


@dataclass(frozen=True)
class _PreparedAgentRun:
    """Prepared agent invocation state after history planning."""

    agent: Agent
    messages: tuple[Message, ...]
    unseen_event_ids: list[str]
    prepared_history: PreparedHistoryState
    # Configured model name resolved at preparation time. Run metadata must use
    # this snapshot instead of re-resolving, because the per-thread override
    # store can change mid-run (for example via the thread_model tool).
    runtime_model_name: str

    @property
    def prompt_text(self) -> str:
        """Return the prompt-visible text derived from canonical live messages."""
        return render_prepared_messages_text(self.messages)

    @property
    def run_input(self) -> list[Message]:
        """Return a deep-copied mutable message list for one provider call."""
        return ai_runtime.copy_run_input(self.messages)


@dataclass
class _MediaAttempt:
    """Per-attempt media routing state shared by the blocking and streaming agent runs."""

    context_media_kinds: frozenset[MediaKind]
    media_route: ModelMediaRoute | None
    removed_media_kinds: frozenset[MediaKind]
    attempt_prompt: list[Message]
    attempt_media_inputs: MediaInputs
    attempt_run_id: str | None

    @property
    def remaining_context_media_kinds(self) -> frozenset[MediaKind]:
        """Return the context media kinds still present after fallback removals."""
        return self.context_media_kinds - self.removed_media_kinds

    @classmethod
    def initial(
        cls,
        run_input: list[Message],
        media_inputs: MediaInputs,
        model: Model | None,
        *,
        fallback_prompt: str,
        run_id: str | None,
    ) -> _MediaAttempt:
        """Route media for the first attempt and build its prompt and inputs."""
        context_media_kinds = ai_runtime.media_inputs_from_run_input(run_input).kinds()
        media_route = build_model_media_route(model) if media_inputs.has_any() or context_media_kinds else None
        media_filter = filter_media_inputs_for_route(media_route, media_inputs)
        removed_media_kinds = media_filter.removed_kinds | (
            unsupported_media_kinds_for_route(media_route) & context_media_kinds
        )
        attempt_prompt = (
            ai_runtime.append_inline_media_fallback_to_run_input(
                run_input,
                fallback_prompt=fallback_prompt,
                removed_kinds=removed_media_kinds,
            )
            if removed_media_kinds
            else ai_runtime.copy_run_input(run_input)
        )
        return cls(
            context_media_kinds=context_media_kinds,
            media_route=media_route,
            removed_media_kinds=removed_media_kinds,
            attempt_prompt=attempt_prompt,
            attempt_media_inputs=media_filter.media_inputs,
            attempt_run_id=run_id,
        )

    def retry(
        self,
        run_input: list[Message],
        *,
        fallback_prompt: str,
        extra_removed_kinds: frozenset[MediaKind],
        retry_media_inputs: MediaInputs,
        run_id: str | None,
    ) -> None:
        """Apply one media-fallback retry: widen removed kinds and rebuild the attempt prompt."""
        self.removed_media_kinds = self.removed_media_kinds | extra_removed_kinds
        self.attempt_prompt = ai_runtime.append_inline_media_fallback_to_run_input(
            run_input,
            fallback_prompt=fallback_prompt,
            removed_kinds=self.removed_media_kinds,
        )
        self.attempt_media_inputs = retry_media_inputs
        self.attempt_run_id = ai_runtime.next_retry_run_id(run_id)


def _prompt_current_sender_id(user_id: str | None, *, include_openai_compat_guidance: bool) -> str | None:
    """Return the Matrix current sender ID when prompt formatting should include it."""
    if include_openai_compat_guidance:
        return None
    return user_id


def _build_timing_scope(
    *,
    reply_to_event_id: str | None,
    run_id: str | None,
    session_id: str,
    agent_name: str,
) -> str:
    """Return one short identifier for correlating AI timing logs."""
    for candidate in (reply_to_event_id, run_id, session_id, agent_name):
        if candidate:
            return candidate[:20]
    return "unknown"


def _reset_turn_state_for_dynamic_continuation(
    *,
    turn_recorder: TurnRecorder | None,
    run_metadata: dict[str, Any] | None,
    completed_tools_for_turn: Sequence[ToolTraceEntry],
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


def _advance_dynamic_continuation(
    *,
    agent: Agent | None,
    scope_context: ScopeSessionContext | None,
    continuation_state: _DynamicContinuationRunState,
    next_prompt: str | None,
    previous_run_id: str | None,
    turn_recorder: TurnRecorder | None,
    run_metadata: dict[str, Any] | None,
    completed_tools_for_turn: Sequence[ToolTraceEntry],
) -> tuple[_DynamicContinuationRunState, AITurnState]:
    """Close the spent agent and prepare run state for one more continuation.

    Shared by the blocking and streaming loops so the continuation handoff stays
    identical across both paths.
    """
    close_agent_runtime_state_dbs(
        agent,
        shared_scope_storage=scope_context.storage if scope_context is not None else None,
    )
    advanced_state = continuation_state.advance(
        continuation_prompt=next_prompt or continuation_state.original_prompt,
        previous_run_id=previous_run_id,
    )
    turn_state = _reset_turn_state_for_dynamic_continuation(
        turn_recorder=turn_recorder,
        run_metadata=run_metadata,
        completed_tools_for_turn=completed_tools_for_turn,
    )
    return advanced_state, turn_state


@timed("system_prompt_assembly.system_enrichment_render")
def _render_system_enrichment_context(
    system_enrichment_items: Sequence[EnrichmentItem],
) -> str:
    return render_system_enrichment_block(system_enrichment_items)


@timed("system_prompt_assembly.compaction_token_breakdown")
def _compute_compaction_token_breakdown(agent: Agent, full_prompt: str) -> dict[str, int]:
    return compute_prompt_token_breakdown(agent=agent, full_prompt=full_prompt)


@dataclass
class _StreamingAttemptState:
    assistant_text: str = ""
    full_response: str = ""
    tool_count: int = 0
    observed_tool_calls: int = 0
    observed_request_metric_fields: set[str] = field(default_factory=set)
    tool_tracker: StreamingToolTracker = field(default_factory=StreamingToolTracker)
    latest_model_id: str | None = None
    latest_model_provider: str | None = None
    latest_request_input_tokens: int | None = None
    latest_request_cache_read_tokens: int | None = None
    latest_request_cache_write_tokens: int | None = None
    cancelled_run_event: RunCancelledEvent | None = None
    completed_run_event: RunCompletedEvent | None = None
    canonical_final_body_candidate: str | None = None
    completed_tool_executions: list[ToolExecution] = field(default_factory=list)
    request_metric_totals: dict[str, int] = field(default_factory=empty_request_metric_totals)
    first_token_latency: float | None = None
    first_token_logged: bool = False
    media_fallback_retry_requested: bool = False
    media_fallback_retry_inputs: MediaInputs | None = None
    media_fallback_removed_kinds: frozenset[MediaKind] = frozenset()
    user_error: Exception | None = None
    stream_exception: Exception | None = None

    @property
    def pending_tools(self) -> list[Any]:
        return self.tool_tracker.pending_tools

    @property
    def completed_tools(self) -> list[ToolTraceEntry]:
        return self.tool_tracker.completed_tools


@dataclass(frozen=True)
class _AgentRunContext:
    """Prepared state shared by one top-level agent response lifecycle."""

    agent_name: str
    prompt: str
    session_id: str
    model_prompt: str | None
    room_id: str | None
    thread_id: str | None
    reply_to_event_id: str | None
    requester_id: str | None
    correlation_id: str
    prepared_run: _PreparedAgentRun
    run_input: list[Message]
    metadata: dict[str, Any] | None
    run_extra_content: dict[str, Any] | None
    inline_media_fallback_prompt: str


@dataclass(frozen=True)
class _NonStreamingAttemptResult:
    """Result of running one non-streaming agent attempt sequence."""

    response: RunOutput | None
    attempt: _MediaAttempt
    user_error: Exception | None = None


@dataclass
class _CollectedStreamResponseState:
    """State for collecting stream-shaped output into one final body."""

    full_response: str = ""
    canonical_final_body_candidate: str | None = None
    tool_tracker: StreamingToolTracker = field(default_factory=StreamingToolTracker)
    tool_trace: list[ToolTraceEntry] = field(default_factory=list)


def _collect_stream_content_chunk(
    state: _CollectedStreamResponseState,
    chunk: str | RunContentEvent | RunCompletedEvent,
) -> None:
    """Append regular stream content or remember the final canonical body."""
    if isinstance(chunk, str):
        state.full_response += chunk
    elif isinstance(chunk, RunContentEvent):
        if chunk.content:
            state.full_response += str(chunk.content)
    elif chunk.content is not None:
        state.canonical_final_body_candidate = str(chunk.content)


def _collect_stream_tool_started(
    state: _CollectedStreamResponseState,
    event: ToolCallStartedEvent,
    *,
    show_tool_calls: bool,
) -> None:
    """Track a tool start while collecting a silent stream."""
    if not show_tool_calls or event.tool is None:
        return

    tool_index = len(state.tool_trace) + 1
    text_chunk, trace_entry = state.tool_tracker.start(event.tool, tool_index=tool_index)
    if trace_entry is not None:
        state.tool_trace.append(trace_entry)
    state.full_response += text_chunk


def _collect_stream_tool_completed(
    state: _CollectedStreamResponseState,
    event: ToolCallCompletedEvent,
    *,
    show_tool_calls: bool,
) -> None:
    """Track a tool completion while collecting a silent stream."""
    if not show_tool_calls:
        return

    completion = state.tool_tracker.complete(event.tool)
    if completion is None:
        return

    tool_name, result, pending_tool, completed_trace = completion
    if pending_tool is None or pending_tool.visible_tool_index is None:
        logger.warning(
            "Missing pending tool start in collected streaming response; skipping completion marker",
            tool_name=tool_name,
        )
        return

    state.full_response, _ = complete_pending_tool_block(
        state.full_response,
        tool_name,
        result,
        tool_index=pending_tool.visible_tool_index,
    )
    if not state.tool_tracker.update_visible_trace_entry(state.tool_trace, pending_tool, completed_trace):
        logger.warning(
            "Missing tool trace slot in collected streaming response for completion",
            tool_name=tool_name,
            tool_index=pending_tool.visible_tool_index,
            trace_len=len(state.tool_trace),
        )


async def _collect_streamed_response_content(
    response_stream: AsyncIterator[AIStreamChunk],
    *,
    show_tool_calls: bool,
    tool_trace_collector: list[ToolTraceEntry] | None = None,
) -> str:
    """Collect a streaming response into one final body without Matrix edits."""
    state = _CollectedStreamResponseState()

    async for chunk in response_stream:
        if isinstance(chunk, str | RunContentEvent | RunCompletedEvent):
            _collect_stream_content_chunk(state, chunk)
        elif isinstance(chunk, ToolCallStartedEvent):
            _collect_stream_tool_started(state, chunk, show_tool_calls=show_tool_calls)
        elif isinstance(chunk, ToolCallCompletedEvent):
            _collect_stream_tool_completed(state, chunk, show_tool_calls=show_tool_calls)

    if tool_trace_collector is not None:
        tool_trace_collector.extend(state.tool_trace)
    return state.full_response or state.canonical_final_body_candidate or ""


def _extract_response_content(response: RunOutput, *, show_tool_calls: bool = True) -> str:
    response_parts = []

    # Add main content if present
    if response.content:
        response_parts.append(response.content)

    # Add formatted tool call sections when present (and enabled).
    if show_tool_calls and response.tools:
        tool_sections: list[str] = []
        for tool_index, tool in enumerate(response.tools, start=1):
            tool_name = tool.tool_name or "tool"
            tool_args = tool.tool_args or {}
            combined, _ = format_tool_combined(tool_name, tool_args, tool.result, tool_index=tool_index)
            tool_sections.append(combined.strip())
        if tool_sections:
            response_parts.append("\n\n".join(tool_sections))

    return "\n".join(response_parts) if response_parts else ""


def _run_error_event_text(event: RunErrorEvent) -> str:
    """Return the best available error text for an Agno streaming error event."""
    if event.content:
        return event.content

    additional_message = _run_error_additional_message(event.additional_data or {})
    if additional_message:
        return additional_message

    details = []
    if event.error_type:
        details.append(f"type={event.error_type}")
    if event.error_id:
        details.append(f"id={event.error_id}")
    if details:
        return f"Agent run failed ({', '.join(details)})"

    return "Agent run failed without provider error details"


def _run_error_additional_message(data: object) -> str | None:
    if isinstance(data, str):
        stripped = data.strip()
        return stripped or None
    if isinstance(data, Mapping):
        mapping = cast("Mapping[object, object]", data)
        for key in ("message", "error", "detail"):
            message = _run_error_additional_message(mapping.get(key))
            if message:
                return message
    return None


def _extract_replayable_response_text(response: RunOutput) -> str:
    """Return canonical assistant text without inline tool-rendering duplication."""
    return _extract_response_content(response, show_tool_calls=False)


def _extract_tool_trace(response: RunOutput) -> list[ToolTraceEntry]:
    """Extract structured tool-trace metadata from a RunOutput."""
    if not response.tools:
        return []

    trace: list[ToolTraceEntry] = []
    for tool in response.tools:
        tool_name = tool.tool_name or "tool"
        tool_args = {str(k): v for k, v in tool.tool_args.items()} if isinstance(tool.tool_args, dict) else {}
        _, trace_entry = format_tool_combined(tool_name, tool_args, tool.result)
        trace.append(trace_entry)
    return trace


def _extract_cancelled_tool_trace(response: RunOutput) -> tuple[list[ToolTraceEntry], list[ToolTraceEntry]]:
    """Extract completed and unfinished tool traces from an interrupted RunOutput."""
    return split_interrupted_tool_trace(response.tools)


def _stream_attempt_has_progress(state: _StreamingAttemptState) -> bool:
    """Return whether one streaming attempt already observed agent-visible work."""
    return bool(state.assistant_text or state.observed_tool_calls)


def _is_run_cancelled_boilerplate(content: str) -> bool:
    """Return whether one string is just Agno cancellation boilerplate."""
    normalized = content.strip().lower()
    return normalized.startswith("run ") and "cancel" in normalized


def _extract_interrupted_partial_text(
    content: object,
    *,
    messages: list[Message] | None = None,
) -> str:
    """Extract assistant partial text while dropping bare cancellation boilerplate."""
    preferred_assistant_parts = [
        str(message.content).strip()
        for message in messages or []
        if (
            isinstance(message, Message)
            and message.role == "assistant"
            and isinstance(message.content, str)
            and not message.from_history
        )
    ]
    assistant_parts = [
        str(message.content).strip()
        for message in messages or []
        if isinstance(message, Message) and message.role == "assistant" and isinstance(message.content, str)
    ]
    candidate_assistant_parts = preferred_assistant_parts or assistant_parts
    for part in reversed(candidate_assistant_parts):
        if part and not _is_run_cancelled_boilerplate(part):
            return part
    if not isinstance(content, str):
        return ""
    stripped = content.strip()
    if _is_run_cancelled_boilerplate(stripped):
        return ""
    return stripped


def _raise_agent_run_cancelled(reason: str | None) -> NoReturn:
    """Raise the canonical agent cancellation error."""
    raise build_cancelled_error(reason)


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


def resolve_run_correlation_id(
    correlation_id: str | None,
    *,
    reply_to_event_id: str | None,
    matrix_run_metadata: dict[str, Any] | None,
) -> str:
    """Return the authoritative correlation ID for one persisted model run."""
    if correlation_id:
        return correlation_id
    metadata_correlation_id = matrix_run_metadata.get("correlation_id") if matrix_run_metadata is not None else None
    if isinstance(metadata_correlation_id, str) and metadata_correlation_id:
        return metadata_correlation_id
    if reply_to_event_id:
        return reply_to_event_id
    return uuid4().hex


def _request_stream_retry(
    state: _StreamingAttemptState,
    *,
    retried_after_media_fallback: bool,
    media_route: ModelMediaRoute | None,
    media_inputs: MediaInputs,
    context_media_kinds: frozenset[MediaKind],
    error: Exception | str,
    log_message: str,
    agent_name: str,
) -> bool:
    """Set retry flag when inline-media fallback should be attempted."""
    if retried_after_media_fallback or _stream_attempt_has_progress(state):
        # Once any stream content is emitted, retrying would duplicate partial output.
        return False
    retry_decision = retry_media_inputs_after_failure(
        media_route,
        error,
        media_inputs,
        extra_present_kinds=context_media_kinds,
    )
    if not retry_decision.should_retry:
        return False
    state.media_fallback_retry_requested = True
    state.media_fallback_retry_inputs = retry_decision.media_inputs
    state.media_fallback_removed_kinds = retry_decision.removed_kinds
    logger.warning(
        log_message,
        agent=agent_name,
        error=str(error),
        removed_media_kinds=sorted(retry_decision.removed_kinds),
    )
    return True


def _track_stream_tool_started(
    state: _StreamingAttemptState,
    event: ToolCallStartedEvent,
    *,
    show_tool_calls: bool,
) -> None:
    """Track started tool-call metadata for streaming output."""
    state.observed_tool_calls += 1
    display_tool_index = state.tool_count + 1 if show_tool_calls else None
    tool_msg, _ = state.tool_tracker.start(event.tool, tool_index=display_tool_index)
    if not show_tool_calls or display_tool_index is None:
        return

    state.tool_count = display_tool_index
    if tool_msg:
        state.full_response += tool_msg


def _track_stream_tool_completed(
    state: _StreamingAttemptState,
    event: ToolCallCompletedEvent,
    *,
    show_tool_calls: bool,
    agent_name: str,
) -> None:
    """Track completed tool-call metadata for streaming output."""
    if event.tool is not None:
        state.completed_tool_executions.append(event.tool)
    completion = state.tool_tracker.complete(event.tool)
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


def _track_model_request_metrics(
    state: _StreamingAttemptState,
    event: ModelRequestCompletedEvent,
) -> None:
    """Track per-request model/token usage for streamed runs."""
    if event.model:
        state.latest_model_id = event.model
    if event.model_provider:
        state.latest_model_provider = event.model_provider
    if isinstance(event.input_tokens, int):
        state.observed_request_metric_fields.add("input_tokens")
        state.latest_request_input_tokens = event.input_tokens
        state.request_metric_totals["input_tokens"] += event.input_tokens
    if isinstance(event.output_tokens, int):
        state.observed_request_metric_fields.add("output_tokens")
        state.request_metric_totals["output_tokens"] += event.output_tokens
    if isinstance(event.total_tokens, int):
        state.observed_request_metric_fields.add("total_tokens")
        state.request_metric_totals["total_tokens"] += event.total_tokens
    if isinstance(event.reasoning_tokens, int):
        state.observed_request_metric_fields.add("reasoning_tokens")
        state.request_metric_totals["reasoning_tokens"] += event.reasoning_tokens
    if isinstance(event.cache_read_tokens, int):
        state.observed_request_metric_fields.add("cache_read_tokens")
        state.request_metric_totals["cache_read_tokens"] += event.cache_read_tokens
    if isinstance(event.cache_write_tokens, int):
        state.observed_request_metric_fields.add("cache_write_tokens")
        state.request_metric_totals["cache_write_tokens"] += event.cache_write_tokens
    state.latest_request_cache_read_tokens = (
        event.cache_read_tokens if isinstance(event.cache_read_tokens, int) else None
    )
    state.latest_request_cache_write_tokens = (
        event.cache_write_tokens if isinstance(event.cache_write_tokens, int) else None
    )
    if state.first_token_latency is None and isinstance(event.time_to_first_token, (int, float)):
        state.first_token_latency = float(event.time_to_first_token)


def _stream_completed_without_visible_output(state: _StreamingAttemptState) -> bool:
    visible_text = state.full_response.strip() or (state.canonical_final_body_candidate or "").strip()
    return state.completed_run_event is not None and not visible_text and state.observed_tool_calls == 0


def _metrics_comparison_payload(metrics: Metrics | dict[str, Any] | None) -> dict[str, Any] | None:
    if metrics is None:
        return None
    if isinstance(metrics, Metrics):
        metrics_dict = metrics.to_dict()
        return metrics_dict if isinstance(metrics_dict, dict) else None
    return metrics


def _usage_metric_int(metrics: Metrics | dict[str, Any] | None, key: str) -> int | None:
    payload = _metrics_comparison_payload(metrics)
    if payload is None:
        return None
    value = payload.get(key)
    return value if isinstance(value, int) else None


def _request_metrics_are_more_complete(
    completed_metrics: Metrics | dict[str, Any] | None,
    request_metrics: dict[str, Any] | None,
) -> bool:
    if request_metrics is None:
        return False
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        request_value = _usage_metric_int(request_metrics, key)
        completed_value = _usage_metric_int(completed_metrics, key)
        if request_value is not None and completed_value is not None and request_value > completed_value:
            return True
    return False


def _select_streaming_usage_metrics(
    completed_metrics: Metrics | None,
    request_metrics: dict[str, Any] | None,
) -> tuple[Metrics | dict[str, Any] | None, dict[str, Any] | None]:
    if completed_metrics is None:
        return request_metrics, None
    if _request_metrics_are_more_complete(completed_metrics, request_metrics):
        return request_metrics, completed_metrics.to_dict()
    return completed_metrics, request_metrics


def _attempt_request_log_context(
    *,
    agent_id: str,
    session_id: str,
    room_id: str | None,
    thread_id: str | None,
    reply_to_event_id: str | None,
    requester_id: str | None,
    correlation_id: str,
    prompt: str,
    model_prompt: str | None,
    attempt_prompt: ai_runtime.ModelRunInput,
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    """Build request-log context for the exact prompt used by one provider attempt."""
    return build_llm_request_log_context(
        agent_id=agent_id,
        session_id=session_id,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        requester_id=requester_id,
        correlation_id=correlation_id,
        prompt=prompt,
        model_prompt=model_prompt,
        full_prompt=render_prepared_messages_text(ai_runtime.copy_run_input(attempt_prompt)),
        metadata=metadata,
    )


@timed("model_request_to_completion")
async def _run_cached_agent_attempt(
    agent: Agent,
    run_input: ai_runtime.ModelRunInput,
    session_id: str,
    *,
    user_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    media: MediaInputs | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunOutput:
    """Run one non-streaming Agno request with timing instrumentation."""
    return await ai_runtime.cached_agent_run(
        agent,
        run_input,
        session_id,
        user_id=user_id,
        run_id=run_id,
        run_id_callback=run_id_callback,
        media=media,
        metadata=metadata,
    )


async def _run_non_streaming_agent_attempts(
    *,
    run_context: _AgentRunContext,
    attempt: _MediaAttempt,
    user_id: str | None,
    run_id: str | None,
    run_id_callback: Callable[[str], None] | None,
    scope_context: ScopeSessionContext | None,
    pipeline_timing: DispatchPipelineTiming | None,
) -> _NonStreamingAttemptResult:
    """Run one non-streaming agent response sequence, including media fallback."""
    agent = run_context.prepared_run.agent
    response: RunOutput | None = None
    try:
        for retried_after_media_fallback in (False, True):
            response = None
            try:
                if pipeline_timing is not None:
                    pipeline_timing.mark("model_request_sent", overwrite=True)
                with bind_llm_request_log_context(
                    **_attempt_request_log_context(
                        agent_id=run_context.agent_name,
                        session_id=run_context.session_id,
                        room_id=run_context.room_id,
                        thread_id=run_context.thread_id,
                        reply_to_event_id=run_context.reply_to_event_id,
                        requester_id=run_context.requester_id,
                        correlation_id=run_context.correlation_id,
                        prompt=run_context.prompt,
                        model_prompt=run_context.model_prompt,
                        attempt_prompt=attempt.attempt_prompt,
                        metadata=run_context.metadata,
                    ),
                ):
                    response = await _run_cached_agent_attempt(
                        agent,
                        attempt.attempt_prompt,
                        run_context.session_id,
                        user_id=user_id,
                        run_id=attempt.attempt_run_id,
                        run_id_callback=run_id_callback,
                        media=attempt.attempt_media_inputs,
                        metadata=run_context.metadata,
                    )
            except Exception as e:
                retry_decision = retry_media_inputs_after_failure(
                    attempt.media_route,
                    e,
                    attempt.attempt_media_inputs,
                    extra_present_kinds=attempt.remaining_context_media_kinds,
                )
                if not retried_after_media_fallback and retry_decision.should_retry:
                    logger.warning(
                        "Retrying AI response after inline media validation error",
                        agent=run_context.agent_name,
                        error=str(e),
                        removed_media_kinds=sorted(retry_decision.removed_kinds),
                    )
                    attempt.retry(
                        run_context.run_input,
                        fallback_prompt=run_context.inline_media_fallback_prompt,
                        extra_removed_kinds=retry_decision.removed_kinds,
                        retry_media_inputs=retry_decision.media_inputs,
                        run_id=run_id,
                    )
                    continue

                logger.exception("Error generating AI response", agent=run_context.agent_name)
                return _NonStreamingAttemptResult(response=None, attempt=attempt, user_error=e)

            if response.status == RunStatus.error:
                error_text = str(response.content or "Unknown agent error")
                retry_decision = retry_media_inputs_after_failure(
                    attempt.media_route,
                    error_text,
                    attempt.attempt_media_inputs,
                    extra_present_kinds=attempt.remaining_context_media_kinds,
                )
                if not retried_after_media_fallback and retry_decision.should_retry:
                    logger.warning(
                        "Retrying AI response after inline media errored run output",
                        agent=run_context.agent_name,
                        error=error_text,
                        removed_media_kinds=sorted(retry_decision.removed_kinds),
                    )
                    attempt.retry(
                        run_context.run_input,
                        fallback_prompt=run_context.inline_media_fallback_prompt,
                        extra_removed_kinds=retry_decision.removed_kinds,
                        retry_media_inputs=retry_decision.media_inputs,
                        run_id=run_id,
                    )
                    continue

                logger.warning(
                    "AI response returned errored run output",
                    agent=run_context.agent_name,
                    error=error_text,
                )

            break

        assert response is not None
        return _NonStreamingAttemptResult(response=response, attempt=attempt)
    finally:
        ai_runtime.cleanup_queued_notice_state(
            run_output=response,
            storage=scope_context.storage if scope_context is not None else None,
            session_id=run_context.session_id,
            session_type=SessionType.AGENT,
            entity_name=run_context.agent_name,
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


def _current_sender_id_kwargs(
    user_id: str | None,
    *,
    include_openai_compat_guidance: bool,
) -> dict[str, str | None]:
    """Return prompt-preparation kwargs without Matrix sender metadata for OpenAI-compatible calls."""
    if include_openai_compat_guidance:
        return {"current_sender_id": None}
    return {
        "current_sender_id": _prompt_current_sender_id(
            user_id,
            include_openai_compat_guidance=include_openai_compat_guidance,
        ),
    }


def _mark_pipeline_timing(pipeline_timing: DispatchPipelineTiming | None, label: str) -> None:
    """Record one dispatch timing mark when turn-level timing is available."""
    if pipeline_timing is not None:
        pipeline_timing.mark(label)


@timed("system_prompt_assembly")
async def _prepare_agent_and_prompt(
    agent_name: str,
    prompt: str,
    runtime_paths: RuntimePaths,
    config: Config,
    session_id: str | None = None,
    scope_context: ScopeSessionContext | None = None,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    room_id: str | None = None,
    thread_id: str | None = None,
    knowledge: Knowledge | None = None,
    include_interactive_questions: bool = True,
    reply_to_event_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    system_enrichment_items: Sequence[EnrichmentItem] = (),
    include_openai_compat_guidance: bool = False,
    model_prompt: str | None = None,
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    current_sender_id: str | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> _PreparedAgentRun:
    """Prepare agent and full prompt for AI processing.

    Returns the prepared run input plus history bookkeeping for one agent turn.
    """
    _assert_agent_target(agent_name, config)
    storage_path = runtime_paths.storage_root
    _mark_pipeline_timing(pipeline_timing, "memory_prepare_start")
    prompt_parts = await build_memory_prompt_parts(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )
    current_turn_prompt = _compose_current_turn_prompt(
        raw_prompt=prompt,
        model_prompt=model_prompt,
        prompt_parts=prompt_parts,
    )
    _mark_pipeline_timing(pipeline_timing, "memory_prepare_ready")

    resolved_session_id = session_id
    if resolved_session_id is None and scope_context is not None and scope_context.session is not None:
        resolved_session_id = scope_context.session.session_id

    _mark_pipeline_timing(pipeline_timing, "agent_build_start")

    def _resolve_model_and_build_agent() -> tuple[ResolvedRuntimeModel, Agent]:
        """Resolve the runtime model and build the agent off the event loop (#1260).

        Both walk the filesystem per dispatch: thread-model overrides, workspace
        scaffolding, context-file reads, and session storage opens.
        """
        runtime_model = config.resolve_runtime_model(
            entity_name=agent_name,
            room_id=room_id,
            thread_id=thread_id,
            runtime_paths=runtime_paths,
        )
        agent = create_agent(
            agent_name,
            config,
            runtime_paths,
            session_id=resolved_session_id,
            history_storage=scope_context.storage if scope_context is not None else None,
            active_model_name=runtime_model.model_name,
            knowledge=knowledge,
            include_interactive_questions=include_interactive_questions,
            include_openai_compat_guidance=include_openai_compat_guidance,
            execution_identity=execution_identity,
            delegation_depth=delegation_depth,
            refresh_scheduler=refresh_scheduler,
            dynamic_tool_continuation=True,
        )
        return runtime_model, agent

    runtime_model, agent = await asyncio.to_thread(_resolve_model_and_build_agent)
    _append_additional_context(agent, prompt_parts.session_preamble)
    if system_enrichment_items:
        _append_additional_context(
            agent,
            _render_system_enrichment_context(system_enrichment_items),
        )
    _mark_pipeline_timing(pipeline_timing, "agent_build_ready")

    prepared_execution = await prepare_agent_execution_context(
        scope_context=scope_context,
        agent=agent,
        agent_name=agent_name,
        prompt=current_turn_prompt,
        thread_history=thread_history,
        runtime_paths=runtime_paths,
        config=config,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        compaction_outcomes_collector=compaction_outcomes_collector,
        compaction_lifecycle=compaction_lifecycle,
        current_sender_id=current_sender_id,
        current_timestamp_ms=current_timestamp_ms,
        current_prompt_is_structured=current_prompt_is_structured,
        include_openai_compat_guidance=include_openai_compat_guidance,
        pipeline_timing=pipeline_timing,
    )
    prepared_history = prepared_execution.prepared_history
    if prepared_execution.replay_plan is not None:
        apply_replay_plan(target=agent, replay_plan=prepared_execution.replay_plan)
    unseen_event_ids = prepared_execution.unseen_event_ids
    run_messages = prepared_execution.messages

    if prepared_history.compaction_outcomes:
        breakdown = _compute_compaction_token_breakdown(agent, render_prepared_messages_text(run_messages))
        enriched_outcomes = [replace(o, **breakdown) for o in prepared_history.compaction_outcomes]
        prepared_history = replace(prepared_history, compaction_outcomes=enriched_outcomes)
        if compaction_outcomes_collector is not None:
            compaction_outcomes_collector.clear()
            compaction_outcomes_collector.extend(enriched_outcomes)
    logger.info(
        "Preparing agent and prompt",
        agent=agent_name,
        full_prompt=render_prepared_messages_text(run_messages),
    )
    return _PreparedAgentRun(
        agent=agent,
        messages=run_messages,
        unseen_event_ids=unseen_event_ids,
        prepared_history=prepared_history,
        runtime_model_name=runtime_model.model_name,
    )


async def _prepare_agent_run_context(
    *,
    agent_name: str,
    prompt: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    scope_context: ScopeSessionContext | None,
    thread_history: Sequence[ResolvedVisibleMessage] | None,
    room_id: str | None,
    thread_id: str | None,
    knowledge: Knowledge | None,
    include_interactive_questions: bool,
    include_openai_compat_guidance: bool,
    reply_to_event_id: str | None,
    active_event_ids: Collection[str],
    execution_identity: ToolExecutionIdentity | None,
    compaction_outcomes_collector: list[CompactionOutcome] | None,
    compaction_lifecycle: CompactionLifecycle | None,
    delegation_depth: int,
    refresh_scheduler: KnowledgeRefreshScheduler | None,
    system_enrichment_items: Sequence[EnrichmentItem],
    model_prompt: str | None,
    current_timestamp_ms: float | None,
    current_prompt_is_structured: bool,
    user_id: str | None,
    requester_id: str | None,
    correlation_id: str,
    matrix_run_metadata: dict[str, Any] | None,
    turn_recorder: TurnRecorder | None,
    pipeline_timing: DispatchPipelineTiming | None,
) -> _AgentRunContext:
    """Prepare one agent response lifecycle through metadata assembly."""
    if pipeline_timing is not None:
        pipeline_timing.mark("ai_prepare_start", overwrite=True)
    prepared_run = await _prepare_agent_and_prompt(
        agent_name,
        prompt,
        runtime_paths,
        config,
        session_id,
        scope_context,
        thread_history,
        room_id,
        thread_id,
        knowledge,
        include_interactive_questions=include_interactive_questions,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        execution_identity=execution_identity,
        compaction_outcomes_collector=compaction_outcomes_collector,
        compaction_lifecycle=compaction_lifecycle,
        delegation_depth=delegation_depth,
        refresh_scheduler=refresh_scheduler,
        system_enrichment_items=system_enrichment_items,
        include_openai_compat_guidance=include_openai_compat_guidance,
        model_prompt=model_prompt,
        current_timestamp_ms=current_timestamp_ms,
        current_prompt_is_structured=current_prompt_is_structured,
        **_current_sender_id_kwargs(
            user_id,
            include_openai_compat_guidance=include_openai_compat_guidance,
        ),
        pipeline_timing=pipeline_timing,
    )
    if pipeline_timing is not None:
        pipeline_timing.mark("history_ready", overwrite=True)
        note_prepared_history_timing(pipeline_timing, prepared_run.prepared_history)

    agent = prepared_run.agent
    if agent.model is not None:
        ai_runtime.install_queued_message_notice_hook(
            agent.model,
            notice_text=config.get_prompt("QUEUED_MESSAGE_NOTICE_TEXT"),
        )

    run_extra_content = build_prepared_history_metadata_content(prepared_run.prepared_history)
    metadata = build_matrix_run_metadata(
        reply_to_event_id,
        prepared_run.unseen_event_ids,
        room_id=room_id,
        thread_id=thread_id,
        requester_id=requester_id,
        correlation_id=correlation_id,
        tools_schema=agent_tool_definition_payloads_for_logging(agent) if agent.model is not None else [],
        model_params=model_params_payload(agent.model) if agent.model is not None else {},
        extra_metadata=deep_merge_metadata(matrix_run_metadata, run_extra_content),
    )
    if turn_recorder is not None:
        turn_recorder.set_run_metadata(metadata)

    return _AgentRunContext(
        agent_name=agent_name,
        prompt=prompt,
        session_id=session_id,
        model_prompt=model_prompt,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        requester_id=requester_id,
        correlation_id=correlation_id,
        prepared_run=prepared_run,
        run_input=prepared_run.run_input,
        metadata=metadata,
        run_extra_content=run_extra_content,
        inline_media_fallback_prompt=config.get_prompt("INLINE_MEDIA_FALLBACK_PROMPT"),
    )


async def ai_response(  # noqa: C901, PLR0915
    agent_name: str,
    prompt: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_prompt: str | None = None,
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    thread_id: str | None = None,
    room_id: str | None = None,
    knowledge: Knowledge | None = None,
    user_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    include_interactive_questions: bool = True,
    include_openai_compat_guidance: bool = False,
    media: MediaInputs | None = None,
    reply_to_event_id: str | None = None,
    correlation_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    show_tool_calls: bool = True,
    collect_streamed_response: bool = False,
    tool_trace_collector: list[ToolTraceEntry] | None = None,
    run_metadata_collector: dict[str, Any] | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    system_enrichment_items: Sequence[EnrichmentItem] = (),
    turn_recorder: TurnRecorder | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> str:
    """Generates a response using the specified agno Agent with memory integration.

    Args:
        agent_name: Name of the agent to use
        prompt: User prompt
        session_id: Session ID for conversation tracking
        runtime_paths: Runtime config/storage paths for agent data and config-aware tools
        config: Application configuration
        thread_history: Optional thread history
        model_prompt: Optional model-facing current-turn prompt additions.
        current_timestamp_ms: Optional source Matrix event timestamp in milliseconds.
        current_prompt_is_structured: Whether the current prompt already carries trusted message tags.
        thread_id: Optional resolved Matrix thread ID for request-log correlation and run metadata.
        room_id: Optional Matrix room ID for caller context
        knowledge: Optional shared knowledge base for RAG-enabled agents
        user_id: Matrix user ID of the sender, used by Agno's LearningMachine
        run_id: Explicit Agno run identifier used for graceful stop/cancel handling.
        run_id_callback: Optional callback that receives the active Agno run_id
            before each real run attempt starts.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        include_openai_compat_guidance: Whether to omit Matrix-style sender
            attribution for OpenAI-compatible prompt formatting.
        media: Optional multimodal inputs (audio/images/files/videos)
        reply_to_event_id: Matrix event ID of the triggering message, stored
            in run metadata for unseen message tracking and edit cleanup.
        correlation_id: Stable cross-sink trace ID for this response lifecycle.
        active_event_ids: Live self-authored Matrix event IDs still tracked as
            actively streaming for this bot in the current room.
        show_tool_calls: Whether to include tool call details inline in the response text.
        collect_streamed_response: Whether to use stream-shaped execution internally
            and collect chunks into one final response body.
        tool_trace_collector: Optional list that receives structured tool-trace
            entries from this run.
        run_metadata_collector: Optional mapping that receives versioned
            run/model/token metadata for Matrix message content.
        execution_identity: Request execution identity used to resolve scoped
            agent state, sessions, and memory consistently for this run.
        compaction_outcomes_collector: Optional list that receives completed
            compaction outcomes from required compaction and manual `compact_context`
            tool calls during this run.
        compaction_lifecycle: Optional lifecycle sink for ordered foreground
            compaction notices.
        delegation_depth: Current nested delegation depth for delegated-agent runs.
        refresh_scheduler: Optional runtime-owned shared knowledge refresh scheduler
            passed through to delegated child agents.
        matrix_run_metadata: Optional Matrix-specific run metadata persisted with the run
            for unseen-message tracking, coalesced edit regeneration, and cleanup.
        system_enrichment_items: Optional system-prompt enrichment items for this run.
        model_prompt: Optional model-facing current-turn prompt additions.
        turn_recorder: Optional lifecycle-owned recorder updated with trusted turn state.
        pipeline_timing: Optional dispatch timing collector updated with AI-stage milestones.

    Returns:
        Agent response string

    """
    logger.info("AI request", agent=agent_name, room_id=room_id)
    if collect_streamed_response:
        return await _collect_streamed_response_content(
            stream_agent_response(
                agent_name=agent_name,
                prompt=prompt,
                session_id=session_id,
                runtime_paths=runtime_paths,
                config=config,
                thread_history=thread_history,
                model_prompt=model_prompt,
                current_timestamp_ms=current_timestamp_ms,
                current_prompt_is_structured=current_prompt_is_structured,
                thread_id=thread_id,
                room_id=room_id,
                knowledge=knowledge,
                user_id=user_id,
                run_id=run_id,
                run_id_callback=run_id_callback,
                include_interactive_questions=include_interactive_questions,
                include_openai_compat_guidance=include_openai_compat_guidance,
                media=media,
                reply_to_event_id=reply_to_event_id,
                correlation_id=correlation_id,
                active_event_ids=active_event_ids,
                show_tool_calls=show_tool_calls,
                run_metadata_collector=run_metadata_collector,
                execution_identity=execution_identity,
                compaction_outcomes_collector=compaction_outcomes_collector,
                compaction_lifecycle=compaction_lifecycle,
                delegation_depth=delegation_depth,
                refresh_scheduler=refresh_scheduler,
                matrix_run_metadata=matrix_run_metadata,
                system_enrichment_items=system_enrichment_items,
                turn_recorder=turn_recorder,
                pipeline_timing=pipeline_timing,
            ),
            show_tool_calls=show_tool_calls,
            tool_trace_collector=tool_trace_collector,
        )

    # Bind the timing scope for this turn; asyncio task contexts isolate it and
    # the next turn in a reused task overwrites it.
    timing_scope.set(
        _build_timing_scope(
            reply_to_event_id=reply_to_event_id,
            run_id=run_id,
            session_id=session_id,
            agent_name=agent_name,
        ),
    )
    media_inputs = media or MediaInputs()
    resolved_requester_id = user_id
    resolved_correlation_id = resolve_run_correlation_id(
        correlation_id,
        reply_to_event_id=reply_to_event_id,
        matrix_run_metadata=matrix_run_metadata,
    )
    agent: Agent | None = None
    scope_context: ScopeSessionContext | None = None
    standalone_interrupted_replay_persisted = False
    empty_response_retried = False
    turn_state = AITurnState()
    unseen_event_ids: list[str] = []
    metadata: dict[str, Any] | None = None
    run_extra_content: dict[str, Any] | None = None
    attempt: _MediaAttempt | None = None
    try:
        try:
            _assert_agent_target(agent_name, config)
        except ValueError as e:
            return get_user_friendly_error_message(e, agent_name)

        async def _run_blocking_attempt(continuation_count: int) -> str | None:  # noqa: C901, PLR0911, PLR0912, PLR0915
            """Run one agent attempt; return final text, or None to continue the turn."""
            nonlocal agent, attempt, continuation_state, empty_response_retried, metadata, run_extra_content
            nonlocal standalone_interrupted_replay_persisted, turn_state, unseen_event_ids
            try:
                run_context = await _prepare_agent_run_context(
                    agent_name=agent_name,
                    prompt=continuation_state.active_prompt,
                    session_id=session_id,
                    runtime_paths=runtime_paths,
                    config=config,
                    scope_context=scope_context,
                    thread_history=thread_history,
                    room_id=room_id,
                    thread_id=thread_id,
                    knowledge=knowledge,
                    include_interactive_questions=include_interactive_questions,
                    include_openai_compat_guidance=include_openai_compat_guidance,
                    reply_to_event_id=reply_to_event_id,
                    active_event_ids=active_event_ids,
                    execution_identity=execution_identity,
                    compaction_outcomes_collector=compaction_outcomes_collector,
                    compaction_lifecycle=compaction_lifecycle,
                    delegation_depth=delegation_depth,
                    refresh_scheduler=refresh_scheduler,
                    system_enrichment_items=system_enrichment_items,
                    model_prompt=continuation_state.active_model_prompt,
                    current_timestamp_ms=continuation_state.active_current_timestamp_ms,
                    current_prompt_is_structured=continuation_state.active_current_prompt_is_structured,
                    user_id=user_id,
                    requester_id=resolved_requester_id,
                    correlation_id=resolved_correlation_id,
                    matrix_run_metadata=matrix_run_metadata,
                    turn_recorder=turn_recorder,
                    pipeline_timing=pipeline_timing,
                )
            except Exception as e:
                logger.exception("Error preparing agent", agent=agent_name)
                return get_user_friendly_error_message(e, agent_name)
            prepared_run = run_context.prepared_run
            agent = prepared_run.agent
            run_input = run_context.run_input
            unseen_event_ids = prepared_run.unseen_event_ids
            metadata = run_context.metadata
            run_extra_content = run_context.run_extra_content

            attempt = _MediaAttempt.initial(
                run_input,
                media_inputs,
                agent.model,
                fallback_prompt=run_context.inline_media_fallback_prompt,
                run_id=continuation_state.active_run_id,
            )
            attempt_result = await _run_non_streaming_agent_attempts(
                run_context=run_context,
                attempt=attempt,
                user_id=user_id,
                run_id=continuation_state.active_run_id,
                run_id_callback=run_id_callback,
                scope_context=scope_context,
                pipeline_timing=pipeline_timing,
            )
            attempt = attempt_result.attempt
            if attempt_result.user_error is not None:
                return get_user_friendly_error_message(attempt_result.user_error, agent_name)
            response = cast("RunOutput", attempt_result.response)

            response_tool_trace = _extract_tool_trace(response)
            if tool_trace_collector is not None:
                tool_trace_collector.extend(response_tool_trace)
            if run_metadata_collector is not None:
                run_metadata = build_ai_run_metadata_content(
                    config=config,
                    model_name=prepared_run.runtime_model_name,
                    run_id=response.run_id,
                    session_id=response.session_id or session_id,
                    status=response.status,
                    model=response.model,
                    model_provider=response.model_provider,
                    metrics=response.metrics,
                    context_input_tokens=prepared_run.prepared_history.prepared_context_tokens,
                    tool_count=len(response.tools) if response.tools is not None else 0,
                    prepared_history=prepared_run.prepared_history,
                )
                run_metadata_collector.update(run_metadata)

            if response.status == RunStatus.cancelled:
                partial_text = _extract_interrupted_partial_text(
                    response.content,
                    messages=response.messages,
                )
                completed_tools, interrupted_tools = _extract_cancelled_tool_trace(response)
                if turn_recorder is not None:
                    turn_state.record_interrupted(
                        turn_recorder,
                        run_metadata=metadata,
                        assistant_text=partial_text,
                        completed_tools=completed_tools,
                        interrupted_tools=interrupted_tools,
                    )
                if turn_recorder is None:
                    persist_interrupted_replay(
                        scope_context=scope_context,
                        session_id=response.session_id or session_id,
                        run_id=response.run_id or attempt.attempt_run_id or str(uuid4()),
                        user_message=prompt,
                        partial_text=partial_text,
                        completed_tools=turn_state.completed_tools_for(completed_tools),
                        interrupted_tools=interrupted_tools,
                        run_metadata=metadata,
                        is_team=False,
                    )
                    standalone_interrupted_replay_persisted = True
                _raise_agent_run_cancelled(response.content)
            if response.status == RunStatus.error:
                return get_user_friendly_error_message(
                    Exception(str(response.content or "Unknown agent error")),
                    agent_name,
                )
            if ai_runtime.is_empty_completed_run(response):
                ai_runtime.discard_empty_completed_run(
                    scope_context=scope_context,
                    session_id=response.session_id or session_id,
                    run_id=response.run_id,
                    session_type=SessionType.AGENT,
                    entity_name=agent_name,
                    output_tokens=_usage_metric_int(response.metrics, "output_tokens"),
                )
                # The one-shot retry borrows a dynamic-continuation slot so the
                # outer loop's iteration budget stays authoritative.
                if not empty_response_retried and continuation_count < DYNAMIC_TOOL_CONTINUATION_LIMIT:
                    empty_response_retried = True
                    return None
                if turn_recorder is not None:
                    turn_state.record_completed(
                        turn_recorder,
                        run_metadata=metadata,
                        assistant_text="",
                        completed_tools=[],
                    )
                return ai_runtime.EMPTY_RESPONSE_NOTICE

            continuation_decision = continuation_decision_from_tools(
                response.tools,
                original_prompt=continuation_state.original_prompt,
                continuation_count=continuation_count,
            )
            completed_tools_for_turn = turn_state.completed_tools_for(response_tool_trace)
            if continuation_decision.should_continue:
                continuation_state, turn_state = _advance_dynamic_continuation(
                    agent=agent,
                    scope_context=scope_context,
                    continuation_state=continuation_state,
                    next_prompt=continuation_decision.next_prompt,
                    previous_run_id=attempt.attempt_run_id,
                    turn_recorder=turn_recorder,
                    run_metadata=metadata,
                    completed_tools_for_turn=completed_tools_for_turn,
                )
                agent = None
                return None
            if continuation_decision.limit_message is not None and continuation_decision.continuation is not None:
                logger.warning(
                    "Dynamic tool continuation limit reached",
                    agent=agent_name,
                    function_name=continuation_decision.continuation.function_name,
                    tool_name=continuation_decision.continuation.tool_name,
                    status=continuation_decision.continuation.status,
                )

            response_text = _extract_response_content(response, show_tool_calls=show_tool_calls)
            if continuation_decision.limit_message is not None and not response.content:
                response_text = continuation_decision.limit_message
            if turn_recorder is not None:
                replayable_response_text = _extract_replayable_response_text(response)
                if continuation_decision.limit_message is not None and not response.content:
                    replayable_response_text = continuation_decision.limit_message
                turn_state.record_completed(
                    turn_recorder,
                    run_metadata=metadata,
                    assistant_text=replayable_response_text,
                    completed_tools=response_tool_trace,
                )
            return response_text

        with open_resolved_scope_session_context(
            agent_name=agent_name,
            scope=HistoryScope(kind="agent", scope_id=agent_name),
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
        ) as opened_scope_context:
            scope_context = opened_scope_context
            ai_runtime.scrub_queued_notice_session_context(
                scope_context=scope_context,
                entity_name=agent_name,
            )
            continuation_state = _DynamicContinuationRunState.initial(
                prompt=prompt,
                model_prompt=model_prompt,
                current_timestamp_ms=current_timestamp_ms,
                current_prompt_is_structured=current_prompt_is_structured,
                run_id=run_id,
            )
            for continuation_count in range(DYNAMIC_TOOL_CONTINUATION_LIMIT + 1):
                attempt_text = await _run_blocking_attempt(continuation_count)
                if attempt_text is not None:
                    return attempt_text
            # The continuation loop always returns on its final iteration: at
            # continuation_count == DYNAMIC_TOOL_CONTINUATION_LIMIT the decision
            # carries a limit_message and never asks to continue.
            msg = "dynamic tool continuation loop must return within its iteration budget"
            raise AssertionError(msg)
    except asyncio.CancelledError:
        if turn_recorder is not None:
            turn_state.record_interrupted_from_recorder(
                turn_recorder,
                run_metadata=(
                    metadata
                    if metadata is not None
                    else turn_recorder.run_metadata
                    or build_matrix_run_metadata(
                        reply_to_event_id,
                        unseen_event_ids,
                        room_id=room_id,
                        thread_id=thread_id,
                        requester_id=resolved_requester_id,
                        correlation_id=resolved_correlation_id,
                        extra_metadata=deep_merge_metadata(matrix_run_metadata, run_extra_content),
                    )
                ),
            )
        elif not standalone_interrupted_replay_persisted:
            persist_interrupted_replay(
                scope_context=scope_context,
                session_id=session_id,
                run_id=(attempt.attempt_run_id if attempt is not None else run_id) or str(uuid4()),
                user_message=prompt,
                partial_text="",
                completed_tools=turn_state.completed_tools_for([]),
                interrupted_tools=[],
                run_metadata=metadata
                if metadata is not None
                else build_matrix_run_metadata(
                    reply_to_event_id,
                    unseen_event_ids,
                    room_id=room_id,
                    thread_id=thread_id,
                    requester_id=resolved_requester_id,
                    correlation_id=resolved_correlation_id,
                    extra_metadata=deep_merge_metadata(matrix_run_metadata, run_extra_content),
                ),
                is_team=False,
            )
        raise
    finally:
        close_agent_runtime_state_dbs(
            agent,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )


@timed("model_request_to_completion")
async def _process_stream_events(  # noqa: C901, PLR0912, PLR0915
    stream_generator: AsyncIterator[object],
    *,
    state: _StreamingAttemptState,
    show_tool_calls: bool,
    agent_name: str,
    media_inputs: MediaInputs,
    retried_after_media_fallback: bool,
    media_route: ModelMediaRoute | None,
    context_media_kinds: frozenset[MediaKind],
    state_updated: Callable[[], None] | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> AsyncGenerator[AIStreamChunk, None]:
    """Consume one streaming attempt, yielding chunks and mutating *state*."""
    try:
        async for event in stream_generator:
            if isinstance(event, RunContentEvent):
                if not event.content:
                    continue
                if not state.first_token_logged:
                    state.first_token_logged = True
                    if pipeline_timing is not None:
                        pipeline_timing.mark("model_first_token")
                chunk_text = str(event.content)
                state.assistant_text += chunk_text
                state.full_response += chunk_text
                if state_updated is not None:
                    state_updated()
                yield event
                continue

            if isinstance(event, ToolCallStartedEvent):
                tool_execution = event.tool
                emit_timing_event(
                    "Dispatch tool-call timing",
                    phase="agno_tool_call_started",
                    agent_name=agent_name,
                    tool_name=tool_execution.tool_name if tool_execution is not None else None,
                    tool_call_id=tool_execution_call_id(tool_execution) if tool_execution is not None else None,
                    show_tool_calls=show_tool_calls,
                )
                _track_stream_tool_started(
                    state,
                    event,
                    show_tool_calls=show_tool_calls,
                )
                if state_updated is not None:
                    state_updated()
                yield event
                continue

            if isinstance(event, ToolCallCompletedEvent):
                tool_execution = event.tool
                emit_timing_event(
                    "Dispatch tool-call timing",
                    phase="agno_tool_call_completed",
                    agent_name=agent_name,
                    tool_name=tool_execution.tool_name if tool_execution is not None else None,
                    tool_call_id=tool_execution_call_id(tool_execution) if tool_execution is not None else None,
                    show_tool_calls=show_tool_calls,
                )
                _track_stream_tool_completed(
                    state,
                    event,
                    show_tool_calls=show_tool_calls,
                    agent_name=agent_name,
                )
                if state_updated is not None:
                    state_updated()
                yield event
                continue

            if isinstance(event, ModelRequestCompletedEvent):
                _track_model_request_metrics(state, event)
                continue

            if isinstance(event, RunCompletedEvent):
                state.completed_run_event = event
                if event.content is not None:
                    state.canonical_final_body_candidate = str(event.content)
                    yield event
                    continue
                continue

            if isinstance(event, RunCancelledEvent):
                state.cancelled_run_event = event
                if state_updated is not None:
                    state_updated()
                return

            if isinstance(event, RunErrorEvent):
                error_text = _run_error_event_text(event)
                if _request_stream_retry(
                    state,
                    retried_after_media_fallback=retried_after_media_fallback,
                    media_route=media_route,
                    media_inputs=media_inputs,
                    context_media_kinds=context_media_kinds,
                    error=error_text,
                    log_message="Retrying streaming AI response after inline media run error",
                    agent_name=agent_name,
                ):
                    return
                logger.error("Agent run error during streaming", agent=agent_name, error=error_text)
                state.user_error = Exception(error_text)
                return

            logger.debug("Skipping stream event", event_type=type(event).__name__)
    except Exception as e:
        if _request_stream_retry(
            state,
            retried_after_media_fallback=retried_after_media_fallback,
            media_route=media_route,
            media_inputs=media_inputs,
            context_media_kinds=context_media_kinds,
            error=e,
            log_message="Retrying streaming AI response after inline media stream exception",
            agent_name=agent_name,
        ):
            return
        logger.exception("Error during streaming AI response")
        state.stream_exception = e


async def _stream_agent_attempt_chunks(
    *,
    run_context: _AgentRunContext,
    attempt: _MediaAttempt,
    state: _StreamingAttemptState,
    show_tool_calls: bool,
    user_id: str | None,
    run_id_callback: Callable[[str], None] | None,
    retried_after_media_fallback: bool,
    state_updated: Callable[[], None] | None,
    pipeline_timing: DispatchPipelineTiming | None,
) -> AsyncGenerator[AIStreamChunk, None]:
    """Start and consume one streaming agent attempt."""
    agent = run_context.prepared_run.agent
    try:
        if pipeline_timing is not None:
            pipeline_timing.mark("model_request_sent", overwrite=True)
        ai_runtime.note_attempt_run_id(run_id_callback, attempt.attempt_run_id)
        request_context = _attempt_request_log_context(
            agent_id=run_context.agent_name,
            session_id=run_context.session_id,
            room_id=run_context.room_id,
            thread_id=run_context.thread_id,
            reply_to_event_id=run_context.reply_to_event_id,
            requester_id=run_context.requester_id,
            correlation_id=run_context.correlation_id,
            prompt=run_context.prompt,
            model_prompt=run_context.model_prompt,
            attempt_prompt=attempt.attempt_prompt,
            metadata=run_context.metadata,
        )
        with bind_llm_request_log_context(**request_context):
            prepared_input = ai_runtime.attach_media_to_run_input(
                attempt.attempt_prompt,
                attempt.attempt_media_inputs,
            )
            stream_generator = agent.arun(
                prepared_input,
                session_id=run_context.session_id,
                user_id=user_id,
                run_id=attempt.attempt_run_id,
                stream=True,
                stream_events=True,
                metadata=run_context.metadata,
            )
        stream_generator = stream_with_llm_request_log_context(
            stream_generator,
            request_context=request_context,
        )
        async for stream_chunk in _process_stream_events(
            stream_generator,
            state=state,
            show_tool_calls=show_tool_calls,
            agent_name=run_context.agent_name,
            media_route=attempt.media_route,
            media_inputs=attempt.attempt_media_inputs,
            context_media_kinds=attempt.remaining_context_media_kinds,
            retried_after_media_fallback=retried_after_media_fallback,
            state_updated=state_updated,
            pipeline_timing=pipeline_timing,
        ):
            yield stream_chunk
    except Exception as e:
        if _request_stream_retry(
            state,
            retried_after_media_fallback=retried_after_media_fallback,
            media_route=attempt.media_route,
            media_inputs=attempt.attempt_media_inputs,
            context_media_kinds=attempt.remaining_context_media_kinds,
            error=e,
            log_message="Retrying streaming AI response after inline media validation error",
            agent_name=run_context.agent_name,
        ):
            return
        logger.exception("Error starting streaming AI response")
        state.user_error = e


async def stream_agent_response(  # noqa: C901, PLR0915
    agent_name: str,
    prompt: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_prompt: str | None = None,
    current_timestamp_ms: float | None = None,
    current_prompt_is_structured: bool = False,
    thread_id: str | None = None,
    room_id: str | None = None,
    knowledge: Knowledge | None = None,
    user_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    include_interactive_questions: bool = True,
    include_openai_compat_guidance: bool = False,
    media: MediaInputs | None = None,
    reply_to_event_id: str | None = None,
    correlation_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    show_tool_calls: bool = True,
    run_metadata_collector: dict[str, Any] | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    compaction_lifecycle: CompactionLifecycle | None = None,
    delegation_depth: int = 0,
    refresh_scheduler: KnowledgeRefreshScheduler | None = None,
    matrix_run_metadata: dict[str, Any] | None = None,
    system_enrichment_items: Sequence[EnrichmentItem] = (),
    turn_recorder: TurnRecorder | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> AsyncIterator[AIStreamChunk]:
    """Generate streaming AI response using Agno's streaming API.

    Args:
        agent_name: Name of the agent to use
        prompt: User prompt
        session_id: Session ID for conversation tracking
        runtime_paths: Runtime config/storage paths for agent data and config-aware tools
        config: Application configuration
        thread_history: Optional thread history
        model_prompt: Optional model-facing current-turn prompt additions.
        current_timestamp_ms: Optional source Matrix event timestamp in milliseconds.
        current_prompt_is_structured: Whether the current prompt already carries trusted message tags.
        thread_id: Optional resolved Matrix thread ID for request-log correlation and run metadata.
        room_id: Optional Matrix room ID for caller context
        knowledge: Optional shared knowledge base for RAG-enabled agents
        user_id: Matrix user ID of the sender, used by Agno's LearningMachine
        run_id: Explicit Agno run identifier used for graceful stop/cancel handling.
        run_id_callback: Optional callback that receives the active Agno run_id
            before each real streaming attempt starts.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        include_openai_compat_guidance: Whether to omit Matrix-style sender
            attribution for OpenAI-compatible prompt formatting.
        media: Optional multimodal inputs (audio/images/files/videos)
        reply_to_event_id: Matrix event ID of the triggering message, stored
            in run metadata for unseen message tracking and edit cleanup.
        correlation_id: Stable cross-sink trace ID for this response lifecycle.
        active_event_ids: Live self-authored Matrix event IDs still tracked as
            actively streaming for this bot in the current room.
        show_tool_calls: Whether to include tool call details inline in the streamed response.
        run_metadata_collector: Optional mapping that receives versioned
            run/model/token metadata for Matrix message content.
        execution_identity: Request execution identity used to resolve scoped
            agent state, sessions, and memory consistently for this run.
        compaction_outcomes_collector: Optional list that receives completed
            compaction outcomes from required compaction and manual `compact_context`
            tool calls during this run.
        compaction_lifecycle: Optional lifecycle sink for ordered foreground
            compaction notices.
        delegation_depth: Current nested delegation depth for delegated-agent runs.
        refresh_scheduler: Optional runtime-owned shared knowledge refresh scheduler
            passed through to delegated child agents.
        matrix_run_metadata: Optional Matrix-specific run metadata persisted with the run
            for unseen-message tracking, coalesced edit regeneration, and cleanup.
        system_enrichment_items: Optional system-prompt enrichment items for this run.
        model_prompt: Optional model-facing current-turn prompt additions.
        turn_recorder: Optional lifecycle-owned recorder updated with trusted turn state.
        pipeline_timing: Optional dispatch timing collector updated with AI-stage milestones.

    Yields:
        Streaming chunks/events as they become available

    """
    logger.info("AI streaming request", agent=agent_name, room_id=room_id)
    # Bind the timing scope for this turn; asyncio task contexts isolate it and
    # the next turn in a reused task overwrites it.
    timing_scope.set(
        _build_timing_scope(
            reply_to_event_id=reply_to_event_id,
            run_id=run_id,
            session_id=session_id,
            agent_name=agent_name,
        ),
    )
    media_inputs = media or MediaInputs()
    resolved_requester_id = user_id
    resolved_correlation_id = resolve_run_correlation_id(
        correlation_id,
        reply_to_event_id=reply_to_event_id,
        matrix_run_metadata=matrix_run_metadata,
    )
    agent: Agent | None = None
    scope_context: ScopeSessionContext | None = None
    standalone_interrupted_replay_persisted = False
    empty_response_retried = False
    turn_state = AITurnState()
    unseen_event_ids: list[str] = []
    metadata: dict[str, Any] | None = None
    run_extra_content: dict[str, Any] | None = None
    attempt: _MediaAttempt | None = None
    state = _StreamingAttemptState()

    try:
        try:
            _assert_agent_target(agent_name, config)
        except ValueError as e:
            yield get_user_friendly_error_message(e, agent_name)
            return

        async def _run_streaming_attempt(  # noqa: C901, PLR0912, PLR0915
            continuation_count: int,
        ) -> AsyncGenerator[AIStreamChunk, None]:
            """Stream one agent attempt; set keep_going when the turn should continue."""
            nonlocal agent, attempt, continuation_state, empty_response_retried, keep_going, metadata
            nonlocal run_extra_content, standalone_interrupted_replay_persisted, state, turn_state
            nonlocal unseen_event_ids
            try:
                run_context = await _prepare_agent_run_context(
                    agent_name=agent_name,
                    prompt=continuation_state.active_prompt,
                    session_id=session_id,
                    runtime_paths=runtime_paths,
                    config=config,
                    scope_context=scope_context,
                    thread_history=thread_history,
                    room_id=room_id,
                    thread_id=thread_id,
                    knowledge=knowledge,
                    include_interactive_questions=include_interactive_questions,
                    include_openai_compat_guidance=include_openai_compat_guidance,
                    reply_to_event_id=reply_to_event_id,
                    active_event_ids=active_event_ids,
                    execution_identity=execution_identity,
                    compaction_outcomes_collector=compaction_outcomes_collector,
                    compaction_lifecycle=compaction_lifecycle,
                    delegation_depth=delegation_depth,
                    refresh_scheduler=refresh_scheduler,
                    system_enrichment_items=system_enrichment_items,
                    model_prompt=continuation_state.active_model_prompt,
                    current_timestamp_ms=continuation_state.active_current_timestamp_ms,
                    current_prompt_is_structured=continuation_state.active_current_prompt_is_structured,
                    user_id=user_id,
                    requester_id=resolved_requester_id,
                    correlation_id=resolved_correlation_id,
                    matrix_run_metadata=matrix_run_metadata,
                    turn_recorder=turn_recorder,
                    pipeline_timing=pipeline_timing,
                )
            except Exception as e:
                logger.exception("Error preparing agent for streaming", agent=agent_name)
                yield get_user_friendly_error_message(e, agent_name)
                return
            prepared_run = run_context.prepared_run
            agent = prepared_run.agent
            run_input = run_context.run_input
            unseen_event_ids = prepared_run.unseen_event_ids
            prepared_context_input_tokens = prepared_run.prepared_history.prepared_context_tokens
            metadata = run_context.metadata
            run_extra_content = run_context.run_extra_content

            attempt = _MediaAttempt.initial(
                run_input,
                media_inputs,
                agent.model,
                fallback_prompt=run_context.inline_media_fallback_prompt,
                run_id=continuation_state.active_run_id,
            )
            state = _StreamingAttemptState()

            try:
                for retried_after_media_fallback in (False, True):
                    state = _StreamingAttemptState()

                    def _sync_live_turn_recorder(
                        *,
                        state_ref: _StreamingAttemptState = state,
                        turn_state_ref: AITurnState = turn_state,
                        metadata_ref: dict[str, Any] | None = metadata,
                    ) -> None:
                        turn_state_ref.sync_partial(
                            turn_recorder,
                            run_metadata=metadata_ref,
                            assistant_text=state_ref.assistant_text,
                            completed_tools=state_ref.completed_tools,
                            interrupted_tools=[pending.trace_entry for pending in state_ref.pending_tools],
                        )

                    async for stream_chunk in _stream_agent_attempt_chunks(
                        run_context=run_context,
                        attempt=attempt,
                        state=state,
                        show_tool_calls=show_tool_calls,
                        user_id=user_id,
                        run_id_callback=run_id_callback,
                        retried_after_media_fallback=retried_after_media_fallback,
                        state_updated=_sync_live_turn_recorder,
                        pipeline_timing=pipeline_timing,
                    ):
                        yield stream_chunk

                    if state.media_fallback_retry_requested:
                        attempt.retry(
                            run_input,
                            fallback_prompt=run_context.inline_media_fallback_prompt,
                            extra_removed_kinds=state.media_fallback_removed_kinds,
                            retry_media_inputs=state.media_fallback_retry_inputs or MediaInputs(),
                            run_id=continuation_state.active_run_id,
                        )
                        continue

                    run_error = state.user_error or state.stream_exception
                    if run_error is not None:
                        if state.assistant_text or state.completed_tools or state.pending_tools:
                            interrupted_tools = [pending.trace_entry for pending in state.pending_tools]
                            if turn_recorder is not None:
                                turn_state.record_interrupted(
                                    turn_recorder,
                                    run_metadata=metadata,
                                    assistant_text=state.assistant_text,
                                    completed_tools=state.completed_tools,
                                    interrupted_tools=interrupted_tools,
                                )
                            elif not standalone_interrupted_replay_persisted:
                                persist_interrupted_replay(
                                    scope_context=scope_context,
                                    session_id=session_id,
                                    run_id=attempt.attempt_run_id or str(uuid4()),
                                    user_message=prompt,
                                    partial_text=state.assistant_text,
                                    completed_tools=turn_state.completed_tools_for(state.completed_tools),
                                    interrupted_tools=interrupted_tools,
                                    run_metadata=metadata,
                                    is_team=False,
                                )
                                standalone_interrupted_replay_persisted = True
                        yield get_user_friendly_error_message(run_error, agent_name)
                        return

                    if state.cancelled_run_event is not None:
                        interrupted_tools = [pending.trace_entry for pending in state.pending_tools]
                        if turn_recorder is not None:
                            turn_state.record_interrupted(
                                turn_recorder,
                                run_metadata=metadata,
                                assistant_text=state.assistant_text,
                                completed_tools=state.completed_tools,
                                interrupted_tools=interrupted_tools,
                            )
                        if run_metadata_collector is not None:
                            fallback_metrics = build_model_request_metrics_fallback(
                                state.request_metric_totals,
                                state.first_token_latency,
                                state.observed_request_metric_fields,
                            )
                            cancelled_metadata = build_ai_run_metadata_content(
                                config=config,
                                model_name=prepared_run.runtime_model_name,
                                run_id=state.cancelled_run_event.run_id,
                                session_id=state.cancelled_run_event.session_id or session_id,
                                status=RunStatus.cancelled,
                                model=state.latest_model_id,
                                model_provider=state.latest_model_provider,
                                metrics=fallback_metrics,
                                context_input_tokens=prepared_context_input_tokens,
                                context_raw_input_tokens=state.latest_request_input_tokens,
                                context_cache_read_tokens=state.latest_request_cache_read_tokens,
                                context_cache_write_tokens=state.latest_request_cache_write_tokens,
                                tool_count=state.observed_tool_calls,
                                prepared_history=prepared_run.prepared_history,
                            )
                            run_metadata_collector.update(cancelled_metadata)
                        if turn_recorder is None:
                            persist_interrupted_replay(
                                scope_context=scope_context,
                                session_id=state.cancelled_run_event.session_id or session_id,
                                run_id=state.cancelled_run_event.run_id or attempt.attempt_run_id or str(uuid4()),
                                user_message=prompt,
                                partial_text=state.assistant_text,
                                completed_tools=turn_state.completed_tools_for(state.completed_tools),
                                interrupted_tools=interrupted_tools,
                                run_metadata=metadata,
                                is_team=False,
                            )
                            standalone_interrupted_replay_persisted = True
                        _raise_agent_run_cancelled(state.cancelled_run_event.reason)

                    break

                if _stream_completed_without_visible_output(state) and not state.completed_tool_executions:
                    completed_event = state.completed_run_event
                    assert completed_event is not None
                    ai_runtime.discard_empty_completed_run(
                        scope_context=scope_context,
                        session_id=completed_event.session_id or session_id,
                        run_id=completed_event.run_id,
                        session_type=SessionType.AGENT,
                        entity_name=agent_name,
                        output_tokens=state.request_metric_totals.get("output_tokens"),
                    )
                    # The one-shot retry borrows a dynamic-continuation slot so the
                    # outer loop's iteration budget stays authoritative.
                    if not empty_response_retried and continuation_count < DYNAMIC_TOOL_CONTINUATION_LIMIT:
                        empty_response_retried = True
                        keep_going = True
                        return
                    # The notice must fall through: the run-metadata recording and
                    # recorder completion below still apply to this notice-only turn.
                    yield RunContentEvent(content=ai_runtime.EMPTY_RESPONSE_NOTICE)

                continuation_decision = continuation_decision_from_tools(
                    state.completed_tool_executions,
                    original_prompt=continuation_state.original_prompt,
                    continuation_count=continuation_count,
                )
                completed_tools_for_turn = turn_state.completed_tools_for(state.completed_tools)
                if continuation_decision.should_continue:
                    continuation_state, turn_state = _advance_dynamic_continuation(
                        agent=agent,
                        scope_context=scope_context,
                        continuation_state=continuation_state,
                        next_prompt=continuation_decision.next_prompt,
                        previous_run_id=attempt.attempt_run_id,
                        turn_recorder=turn_recorder,
                        run_metadata=metadata,
                        completed_tools_for_turn=completed_tools_for_turn,
                    )
                    agent = None
                    keep_going = True
                    return
                if continuation_decision.limit_message is not None and continuation_decision.continuation is not None:
                    logger.warning(
                        "Dynamic tool continuation limit reached during streaming",
                        agent=agent_name,
                        function_name=continuation_decision.continuation.function_name,
                        tool_name=continuation_decision.continuation.tool_name,
                        status=continuation_decision.continuation.status,
                    )
                    if not state.assistant_text and not state.canonical_final_body_candidate:
                        state.assistant_text = continuation_decision.limit_message
                        yield RunContentEvent(content=continuation_decision.limit_message)

                if run_metadata_collector is not None:
                    fallback_metrics = build_model_request_metrics_fallback(
                        state.request_metric_totals,
                        state.first_token_latency,
                        state.observed_request_metric_fields,
                    )
                    final_status = (
                        RunStatus.error if _stream_completed_without_visible_output(state) else RunStatus.completed
                    )
                    usage_metrics, usage_metrics_fallback = _select_streaming_usage_metrics(
                        state.completed_run_event.metrics if state.completed_run_event is not None else None,
                        fallback_metrics,
                    )
                    run_metadata = build_ai_run_metadata_content(
                        config=config,
                        model_name=prepared_run.runtime_model_name,
                        run_id=state.completed_run_event.run_id if state.completed_run_event is not None else None,
                        session_id=(
                            state.completed_run_event.session_id
                            if state.completed_run_event is not None
                            and state.completed_run_event.session_id is not None
                            else session_id
                        ),
                        status=final_status,
                        model=state.latest_model_id,
                        model_provider=state.latest_model_provider,
                        metrics=usage_metrics,
                        metrics_fallback=usage_metrics_fallback,
                        context_input_tokens=prepared_context_input_tokens,
                        context_raw_input_tokens=state.latest_request_input_tokens,
                        context_cache_read_tokens=state.latest_request_cache_read_tokens,
                        context_cache_write_tokens=state.latest_request_cache_write_tokens,
                        tool_count=(
                            len(state.completed_run_event.tools)
                            if state.completed_run_event is not None and state.completed_run_event.tools is not None
                            else state.observed_tool_calls
                        ),
                        prepared_history=prepared_run.prepared_history,
                    )
                    run_metadata_collector.update(run_metadata)
                if turn_recorder is not None:
                    final_visible_body = state.assistant_text or state.canonical_final_body_candidate or ""
                    turn_state.record_completed(
                        turn_recorder,
                        run_metadata=metadata,
                        assistant_text=final_visible_body,
                        completed_tools=state.completed_tools,
                    )
                return
            finally:
                ai_runtime.cleanup_queued_notice_state(
                    run_output=None,
                    storage=scope_context.storage if scope_context is not None else None,
                    session_id=session_id,
                    session_type=SessionType.AGENT,
                    entity_name=agent_name,
                )

        with open_resolved_scope_session_context(
            agent_name=agent_name,
            scope=HistoryScope(kind="agent", scope_id=agent_name),
            session_id=session_id,
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=execution_identity,
        ) as opened_scope_context:
            scope_context = opened_scope_context
            ai_runtime.scrub_queued_notice_session_context(
                scope_context=scope_context,
                entity_name=agent_name,
            )
            continuation_state = _DynamicContinuationRunState.initial(
                prompt=prompt,
                model_prompt=model_prompt,
                current_timestamp_ms=current_timestamp_ms,
                current_prompt_is_structured=current_prompt_is_structured,
                run_id=run_id,
            )
            for continuation_count in range(DYNAMIC_TOOL_CONTINUATION_LIMIT + 1):
                keep_going = False
                async for stream_chunk in _run_streaming_attempt(continuation_count):
                    yield stream_chunk
                if not keep_going:
                    return
            # The continuation loop always returns on its final iteration: at
            # continuation_count == DYNAMIC_TOOL_CONTINUATION_LIMIT the decision
            # carries a limit_message and never asks to continue.
            msg = "dynamic tool continuation loop must return within its iteration budget"
            raise AssertionError(msg)
    except asyncio.CancelledError:
        interrupted_tools = [pending.trace_entry for pending in state.pending_tools]
        if turn_recorder is not None:
            turn_state.record_interrupted(
                turn_recorder,
                run_metadata=metadata
                if metadata is not None
                else turn_recorder.run_metadata
                or build_matrix_run_metadata(
                    reply_to_event_id,
                    unseen_event_ids,
                    room_id=room_id,
                    thread_id=thread_id,
                    requester_id=resolved_requester_id,
                    correlation_id=resolved_correlation_id,
                    extra_metadata=deep_merge_metadata(matrix_run_metadata, run_extra_content),
                ),
                assistant_text=state.assistant_text,
                completed_tools=state.completed_tools,
                interrupted_tools=interrupted_tools,
            )
        elif not standalone_interrupted_replay_persisted:
            persist_interrupted_replay(
                scope_context=scope_context,
                session_id=session_id,
                run_id=(attempt.attempt_run_id if attempt is not None else run_id) or str(uuid4()),
                user_message=prompt,
                partial_text=state.assistant_text,
                completed_tools=turn_state.completed_tools_for(state.completed_tools),
                interrupted_tools=interrupted_tools,
                run_metadata=metadata
                if metadata is not None
                else build_matrix_run_metadata(
                    reply_to_event_id,
                    unseen_event_ids,
                    room_id=room_id,
                    thread_id=thread_id,
                    requester_id=resolved_requester_id,
                    correlation_id=resolved_correlation_id,
                    extra_metadata=deep_merge_metadata(matrix_run_metadata, run_extra_content),
                ),
                is_team=False,
            )
        raise
    finally:
        close_agent_runtime_state_dbs(
            agent,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )
