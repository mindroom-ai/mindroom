"""AI integration module for MindRoom agents and memory management."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, NoReturn
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
from mindroom.cancellation import build_cancelled_error
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
    ROUTER_AGENT_NAME,
    RuntimePaths,
)
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import (
    prepare_agent_execution_context,
    render_prepared_messages_text,
)
from mindroom.history import (
    CompactionOutcome,
    PreparedHistoryState,
)
from mindroom.history.compaction import compute_prompt_token_breakdown
from mindroom.history.interrupted_replay import (
    persist_interrupted_replay,
    split_interrupted_tool_trace,
    tool_execution_call_id,
)
from mindroom.history.runtime import (
    ScopeSessionContext,
    apply_replay_plan,
    close_agent_runtime_sqlite_dbs,
    open_resolved_scope_session_context,
)
from mindroom.history.types import HistoryScope
from mindroom.hooks import EnrichmentItem, render_system_enrichment_block
from mindroom.llm_request_logging import (
    bind_llm_request_log_context,
    build_llm_request_log_context,
)
from mindroom.logging_config import get_logger
from mindroom.media_fallback import should_retry_without_inline_media
from mindroom.media_inputs import MediaInputs
from mindroom.memory import MemoryPromptParts, build_memory_prompt_parts, strip_user_turn_time_prefix
from mindroom.timing import DispatchPipelineTiming, timed
from mindroom.tool_system.events import (
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_combined,
    format_tool_completed_event,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Collection, Sequence

    from agno.agent import Agent
    from agno.knowledge.knowledge import Knowledge
    from agno.models.response import ToolExecution

    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.history.turn_recorder import TurnRecorder
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

__all__ = [
    "AIStreamChunk",
    "ai_response",
    "build_matrix_run_metadata",
    "stream_agent_response",
]
AIStreamChunk = str | RunContentEvent | RunCompletedEvent | ToolCallStartedEvent | ToolCallCompletedEvent
_AI_RUN_METADATA_VERSION = 1


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
    normalized_raw_prompt = raw_prompt.strip()
    normalized_model_prompt = model_prompt.strip() if model_prompt else ""
    normalized_model_prompt_without_time = (
        strip_user_turn_time_prefix(normalized_model_prompt) if normalized_model_prompt else ""
    )

    if normalized_raw_prompt:
        prompt_chunks.append(raw_prompt)
        if normalized_model_prompt == normalized_raw_prompt:
            normalized_model_prompt = ""
        elif normalized_model_prompt.startswith(f"{normalized_raw_prompt}\n\n"):
            normalized_model_prompt = normalized_model_prompt[len(normalized_raw_prompt) + 2 :].lstrip()
        elif normalized_model_prompt_without_time == normalized_raw_prompt:
            normalized_model_prompt = ""
        elif normalized_model_prompt_without_time.startswith(f"{normalized_raw_prompt}\n\n"):
            normalized_model_prompt = normalized_model_prompt_without_time[len(normalized_raw_prompt) + 2 :].lstrip()

    if prompt_parts.turn_context:
        prompt_chunks.append(prompt_parts.turn_context)
    if normalized_model_prompt:
        prompt_chunks.append(normalized_model_prompt)

    return "\n\n".join(chunk for chunk in prompt_chunks if chunk)


@dataclass(frozen=True)
class _PreparedAgentRun:
    """Prepared agent invocation state after history planning."""

    agent: Agent
    messages: tuple[Message, ...]
    unseen_event_ids: list[str]
    prepared_history: PreparedHistoryState

    @property
    def prompt_text(self) -> str:
        """Return the prompt-visible text derived from canonical live messages."""
        return render_prepared_messages_text(self.messages)

    @property
    def run_input(self) -> list[Message]:
        """Return a deep-copied mutable message list for one provider call."""
        return ai_runtime.copy_run_input(self.messages)


def _empty_request_metric_totals() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def _next_retry_run_id(run_id: str | None) -> str | None:
    """Return a fresh Agno run identifier for a retry attempt."""
    if run_id is None:
        return None
    return str(uuid4())


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


def _note_attempt_run_id(run_id_callback: Callable[[str], None] | None, run_id: str | None) -> None:
    """Publish the current run_id before starting a real Agno run attempt."""
    if run_id_callback is not None and run_id is not None:
        run_id_callback(run_id)


@timed("system_prompt_assembly.system_enrichment_render")
def _render_system_enrichment_context(
    system_enrichment_items: Sequence[EnrichmentItem],
    *,
    timing_scope: str | None = None,
) -> str:
    del timing_scope
    return render_system_enrichment_block(system_enrichment_items)


@timed("system_prompt_assembly.compaction_token_breakdown")
def _compute_compaction_token_breakdown(
    agent: Agent,
    full_prompt: str,
    *,
    timing_scope: str | None = None,
) -> dict[str, int]:
    del timing_scope
    return compute_prompt_token_breakdown(agent=agent, full_prompt=full_prompt)


@dataclass
class _StreamingAttemptState:
    assistant_text: str = ""
    full_response: str = ""
    tool_count: int = 0
    observed_tool_calls: int = 0
    observed_reasoning_content: bool = False
    pending_tools: list[_PendingStreamingTool] = field(default_factory=list)
    completed_tools: list[ToolTraceEntry] = field(default_factory=list)
    latest_model_id: str | None = None
    latest_model_provider: str | None = None
    latest_request_input_tokens: int | None = None
    cancelled_run_event: RunCancelledEvent | None = None
    completed_run_event: RunCompletedEvent | None = None
    request_metric_totals: dict[str, int] = field(default_factory=_empty_request_metric_totals)
    observed_request_metric_keys: set[str] = field(default_factory=set)
    first_token_latency: float | None = None
    first_token_logged: bool = False
    retry_requested: bool = False
    user_error: Exception | None = None
    stream_exception: Exception | None = None


@dataclass
class _PendingStreamingTool:
    tool_name: str
    trace_entry: ToolTraceEntry
    tool_call_id: str | None = None
    visible_tool_index: int | None = None


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


def _find_matching_pending_stream_tool(
    pending_tools: list[_PendingStreamingTool],
    tool: ToolExecution | None,
) -> int | None:
    """Return the newest pending tool matching a completion event."""
    call_id = tool_execution_call_id(tool)
    if call_id is not None:
        for pos in range(len(pending_tools) - 1, -1, -1):
            if pending_tools[pos].tool_call_id == call_id:
                return pos
    info = extract_tool_completed_info(tool)
    if info is None:
        return None
    tool_name, _ = info
    for pos in range(len(pending_tools) - 1, -1, -1):
        pending_tool = pending_tools[pos]
        if pending_tool.tool_call_id is None and pending_tool.tool_name == tool_name:
            return pos
    return None


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


def _get_model_config(
    config: Config,
    agent_name: str,
    *,
    runtime_paths: RuntimePaths,
    room_id: str | None = None,
) -> tuple[str | None, ModelConfig | None]:
    """Return configured model name/config for an agent when available."""
    if agent_name not in config.agents and agent_name not in config.teams and agent_name != ROUTER_AGENT_NAME:
        return None, None
    model_name = config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    ).model_name
    return model_name, config.models.get(model_name)


def _finalize_usage_payload(
    payload: dict[str, Any],
    *,
    output_tokens_hint: int | None = None,
    total_tokens_hint: int | None = None,
    derive_total_tokens: bool = False,
) -> dict[str, Any] | None:
    if isinstance(output_tokens_hint, int) and "output_tokens" not in payload:
        payload["output_tokens"] = output_tokens_hint
    existing_total_tokens = payload.get("total_tokens")
    should_derive_total_tokens = (
        derive_total_tokens
        and isinstance(payload.get("input_tokens"), int)
        and isinstance(payload.get("output_tokens"), int)
        and (
            "total_tokens" not in payload
            or (existing_total_tokens == 0 and (payload["input_tokens"] > 0 or payload["output_tokens"] > 0))
        )
    )
    if "total_tokens" not in payload or should_derive_total_tokens:
        if isinstance(total_tokens_hint, int) and total_tokens_hint > 0:
            payload["total_tokens"] = total_tokens_hint
        elif derive_total_tokens:
            input_tokens = payload.get("input_tokens")
            output_tokens = payload.get("output_tokens")
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                payload["total_tokens"] = input_tokens + output_tokens
    return payload or None


def _serialize_metrics(metrics: Metrics | dict[str, Any] | None) -> dict[str, Any] | None:
    def _sanitize_metrics_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        sanitized: dict[str, Any] = {}
        for key, value in payload.items():
            if isinstance(value, (str, int)) or value is None or isinstance(value, bool):
                sanitized[key] = value
            elif isinstance(value, float):
                sanitized[key] = format(value, ".12g")
        return sanitized or None

    if metrics is None:
        return None
    if isinstance(metrics, Metrics):
        metrics_dict = metrics.to_dict()
        if not isinstance(metrics_dict, dict):
            return None
        sanitized = _sanitize_metrics_payload(metrics_dict)
        if sanitized is None:
            return None
        return _finalize_usage_payload(
            sanitized,
            output_tokens_hint=metrics.output_tokens,
            total_tokens_hint=metrics.total_tokens,
            derive_total_tokens=True,
        )
    if isinstance(metrics, dict):
        return _sanitize_metrics_payload(metrics)
    return None


def _build_model_request_metrics_fallback(
    totals: dict[str, int],
    first_token_latency: float | None,
    observed_metric_keys: set[str],
) -> dict[str, Any] | None:
    payload = {key: value for key, value in totals.items() if value > 0 or key in observed_metric_keys}
    if first_token_latency is not None:
        payload["time_to_first_token"] = format(first_token_latency, ".12g")
    return _finalize_usage_payload(payload, derive_total_tokens=True)


def _build_context_payload(
    *,
    context_input_tokens: int | None,
    model_config: ModelConfig | None,
) -> dict[str, Any] | None:
    if context_input_tokens is None or model_config is None or model_config.context_window is None:
        return None
    context_window = model_config.context_window
    if context_window <= 0:
        return None
    return {
        "input_tokens": context_input_tokens,
        "window_tokens": context_window,
    }


def _build_ai_run_metadata_content(  # noqa: C901, PLR0912
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    run_id: str | None,
    session_id: str | None,
    status: RunStatus | str | None,
    model: str | None,
    model_provider: str | None,
    room_id: str | None = None,
    metrics: Metrics | dict[str, Any] | None = None,
    metrics_fallback: dict[str, Any] | None = None,
    context_input_tokens: int | None = None,
    tool_count: int | None = None,
) -> dict[str, Any] | None:
    model_name, model_config = _get_model_config(
        config,
        agent_name,
        runtime_paths=runtime_paths,
        room_id=room_id,
    )
    model_id = model or (model_config.id if model_config is not None else None)
    provider = model_provider or (model_config.provider if model_config is not None else None)

    usage_payload = _serialize_metrics(metrics)
    if usage_payload is None and metrics_fallback:
        usage_payload = dict(metrics_fallback)

    usage_input_tokens = usage_payload.get("input_tokens") if usage_payload else None
    if not isinstance(usage_input_tokens, int):
        usage_input_tokens = None

    payload: dict[str, Any] = {"version": _AI_RUN_METADATA_VERSION}
    if run_id is not None:
        payload["run_id"] = run_id
    if session_id is not None:
        payload["session_id"] = session_id
    if status is not None:
        raw_status = status.value if isinstance(status, RunStatus) else str(status)
        payload["status"] = raw_status.lower()
    if model_name is not None or model_id is not None or provider is not None:
        model_payload: dict[str, Any] = {}
        if model_name is not None:
            model_payload["config"] = model_name
        if model_id is not None:
            model_payload["id"] = model_id
        if provider is not None:
            model_payload["provider"] = provider
        if model_payload:
            payload["model"] = model_payload
    if usage_payload:
        payload["usage"] = usage_payload
    context_payload = _build_context_payload(
        context_input_tokens=context_input_tokens if context_input_tokens is not None else usage_input_tokens,
        model_config=model_config,
    )
    if context_payload:
        payload["context"] = context_payload
    if tool_count is not None:
        payload["tools"] = {"count": tool_count}

    if len(payload) == 1:
        return None
    return {AI_RUN_METADATA_KEY: payload}


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
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Build metadata dict for a run, tracking consumed Matrix event ids."""
    if not reply_to_event_id:
        return dict(extra_metadata) if extra_metadata else None
    metadata = dict(extra_metadata or {})
    source_event_ids = _normalized_string_list(metadata.get(MATRIX_SOURCE_EVENT_IDS_METADATA_KEY))
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


def _request_stream_retry(
    state: _StreamingAttemptState,
    *,
    retried_without_inline_media: bool,
    media_inputs: MediaInputs,
    error: Exception | str,
    log_message: str,
    agent_name: str,
) -> bool:
    """Set retry flag when inline-media fallback should be attempted."""
    if retried_without_inline_media or _stream_attempt_has_progress(state):
        # Once any stream content is emitted, retrying would duplicate partial output.
        return False
    if not should_retry_without_inline_media(error, media_inputs):
        return False
    state.retry_requested = True
    logger.warning(
        log_message,
        agent=agent_name,
        error=str(error),
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
    tool_msg, trace_entry = format_tool_started_event(event.tool, tool_index=display_tool_index)
    if trace_entry is not None:
        state.pending_tools.append(
            _PendingStreamingTool(
                tool_name=trace_entry.tool_name,
                trace_entry=trace_entry,
                tool_call_id=tool_execution_call_id(event.tool),
                visible_tool_index=display_tool_index,
            ),
        )
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
    info = extract_tool_completed_info(event.tool)
    if info is None:
        return
    tool_name, result = info
    pending_trace_pos = _find_matching_pending_stream_tool(state.pending_tools, event.tool)
    pending_tool = state.pending_tools.pop(pending_trace_pos) if pending_trace_pos is not None else None
    _, completed_trace = format_tool_completed_event(event.tool)
    if completed_trace is not None:
        state.completed_tools.append(completed_trace)
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
        state.latest_request_input_tokens = event.input_tokens
        state.request_metric_totals["input_tokens"] += event.input_tokens
        state.observed_request_metric_keys.add("input_tokens")
    if isinstance(event.output_tokens, int):
        state.request_metric_totals["output_tokens"] += event.output_tokens
        state.observed_request_metric_keys.add("output_tokens")
    if isinstance(event.total_tokens, int):
        state.request_metric_totals["total_tokens"] += event.total_tokens
        state.observed_request_metric_keys.add("total_tokens")
    if isinstance(event.reasoning_tokens, int):
        state.request_metric_totals["reasoning_tokens"] += event.reasoning_tokens
        state.observed_request_metric_keys.add("reasoning_tokens")
    if isinstance(event.cache_read_tokens, int):
        state.request_metric_totals["cache_read_tokens"] += event.cache_read_tokens
        state.observed_request_metric_keys.add("cache_read_tokens")
    if isinstance(event.cache_write_tokens, int):
        state.request_metric_totals["cache_write_tokens"] += event.cache_write_tokens
        state.observed_request_metric_keys.add("cache_write_tokens")
    if state.first_token_latency is None and isinstance(event.time_to_first_token, (int, float)):
        state.first_token_latency = float(event.time_to_first_token)


def _attempt_request_log_context(
    *,
    session_id: str,
    room_id: str | None,
    thread_id: str | None,
    reply_to_event_id: str | None,
    prompt: str,
    model_prompt: str | None,
    attempt_prompt: ai_runtime.ModelRunInput,
    metadata: dict[str, object] | None,
) -> dict[str, object]:
    """Build request-log context for the exact prompt used by one provider attempt."""
    return build_llm_request_log_context(
        session_id=session_id,
        room_id=room_id,
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        prompt=prompt,
        model_prompt=model_prompt,
        full_prompt=render_prepared_messages_text(ai_runtime.copy_run_input(attempt_prompt)),
        metadata=metadata,
    )


async def _stream_with_request_log_context[StreamEventT](
    stream_generator: AsyncIterator[StreamEventT],
    *,
    request_context: dict[str, object],
) -> AsyncIterator[StreamEventT]:
    """Advance one async stream with request-log context bound per item pull."""
    with bind_llm_request_log_context(**request_context):
        stream_iterator = stream_generator.__aiter__()
    while True:
        try:
            with bind_llm_request_log_context(**request_context):
                event = await stream_iterator.__anext__()
        except StopAsyncIteration:
            return
        yield event


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
    timing_scope: str | None = None,
) -> RunOutput:
    """Run one non-streaming Agno request with timing instrumentation."""
    del timing_scope
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


def _assert_agent_target(agent_name: str, config: Config) -> None:
    """Reject configured team names in the agent-only AI helper path."""
    if agent_name in config.teams:
        msg = (
            f"'{agent_name}' is a configured team, not an agent. "
            "Use the explicit team execution helpers or the OpenAI-compatible model "
            f"'team/{agent_name}' instead."
        )
        raise ValueError(msg)


def _prompt_current_sender_id(
    user_id: str | None,
    *,
    include_openai_compat_guidance: bool,
) -> str | None:
    """Return the sender label to embed in prompt history, if any."""
    if include_openai_compat_guidance:
        return None
    return user_id


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
    knowledge: Knowledge | None = None,
    include_interactive_questions: bool = True,
    reply_to_event_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    delegation_depth: int = 0,
    system_enrichment_items: Sequence[EnrichmentItem] = (),
    current_sender_id: str | None = None,
    include_openai_compat_guidance: bool = False,
    timing_scope: str | None = None,
    model_prompt: str | None = None,
) -> _PreparedAgentRun:
    """Prepare agent and full prompt for AI processing.

    Returns the prepared run input plus history bookkeeping for one agent turn.
    """
    _assert_agent_target(agent_name, config)
    storage_path = runtime_paths.storage_root
    prompt_parts = await build_memory_prompt_parts(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        timing_scope=timing_scope,
    )
    current_turn_prompt = _compose_current_turn_prompt(
        raw_prompt=prompt,
        model_prompt=model_prompt,
        prompt_parts=prompt_parts,
    )

    runtime_model = config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    )
    resolved_session_id = session_id
    if resolved_session_id is None and scope_context is not None and scope_context.session is not None:
        resolved_session_id = scope_context.session.session_id

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
        timing_scope=timing_scope,
    )
    if system_enrichment_items:
        _append_additional_context(
            agent,
            _render_system_enrichment_context(
                system_enrichment_items,
                timing_scope=timing_scope,
            ),
        )
    _append_additional_context(agent, prompt_parts.session_preamble)

    prepared_execution = await prepare_agent_execution_context(
        scope_context=scope_context,
        agent=agent,
        agent_name=agent_name,
        prompt=current_turn_prompt,
        thread_history=thread_history,
        runtime_paths=runtime_paths,
        config=config,
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        compaction_outcomes_collector=compaction_outcomes_collector,
        current_sender_id=current_sender_id,
        timing_scope=timing_scope,
    )
    prepared_history = PreparedHistoryState(
        compaction_outcomes=prepared_execution.compaction_outcomes,
        replay_plan=prepared_execution.replay_plan,
        replays_persisted_history=prepared_execution.replays_persisted_history,
    )
    if prepared_execution.replay_plan is not None:
        apply_replay_plan(target=agent, replay_plan=prepared_execution.replay_plan)
    unseen_event_ids = prepared_execution.unseen_event_ids
    run_messages = prepared_execution.messages

    if prepared_history.compaction_outcomes:
        breakdown = _compute_compaction_token_breakdown(
            agent,
            render_prepared_messages_text(run_messages),
            timing_scope=timing_scope,
        )
        enriched_outcomes = [replace(o, **breakdown) for o in prepared_history.compaction_outcomes]
        prepared_history = PreparedHistoryState(
            compaction_outcomes=enriched_outcomes,
            replay_plan=prepared_history.replay_plan,
            replays_persisted_history=prepared_history.replays_persisted_history,
        )
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
    )


async def ai_response(  # noqa: C901, PLR0912, PLR0915
    agent_name: str,
    prompt: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_prompt: str | None = None,
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
    active_event_ids: Collection[str] = frozenset(),
    show_tool_calls: bool = True,
    tool_trace_collector: list[ToolTraceEntry] | None = None,
    run_metadata_collector: dict[str, Any] | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    delegation_depth: int = 0,
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
        model_prompt: Optional model-facing prompt after caller-side prompt shaping
        thread_id: Optional resolved Matrix thread target for the request
        room_id: Optional Matrix room ID for caller context
        knowledge: Optional shared knowledge base for RAG-enabled agents
        user_id: Matrix user ID of the sender, used by Agno's LearningMachine
        run_id: Explicit Agno run identifier used for graceful stop/cancel handling.
        run_id_callback: Optional callback that receives the active Agno run_id
            before each real run attempt starts.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        include_openai_compat_guidance: Whether to include OpenAI-compatible
            history-format guidance in the shared identity prompt.
        media: Optional multimodal inputs (audio/images/files/videos)
        reply_to_event_id: Matrix event ID of the triggering message, stored
            in run metadata for unseen message tracking and edit cleanup.
        active_event_ids: Live self-authored Matrix event IDs still tracked as
            actively streaming for this bot in the current room.
        show_tool_calls: Whether to include tool call details inline in the response text.
        tool_trace_collector: Optional list that receives structured tool-trace
            entries from this run.
        run_metadata_collector: Optional mapping that receives versioned
            run/model/token metadata for Matrix message content.
        execution_identity: Request execution identity used to resolve scoped
            agent state, sessions, and memory consistently for this run.
        compaction_outcomes_collector: Optional list that receives completed
            compaction outcomes from auto-compaction and manual `compact_context`
            tool calls during this run.
        delegation_depth: Current nested delegation depth for delegated-agent runs.
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
    timing_scope = _build_timing_scope(
        reply_to_event_id=reply_to_event_id,
        run_id=run_id,
        session_id=session_id,
        agent_name=agent_name,
    )
    media_inputs = media or MediaInputs()
    agent: Agent | None = None
    scope_context: ScopeSessionContext | None = None
    standalone_interrupted_replay_persisted = False
    unseen_event_ids: list[str] = []
    attempt_run_id = run_id
    try:
        try:
            _assert_agent_target(agent_name, config)
        except ValueError as e:
            return get_user_friendly_error_message(e, agent_name)
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
            try:
                if pipeline_timing is not None:
                    pipeline_timing.mark("ai_prepare_start")
                prepared_run = await _prepare_agent_and_prompt(
                    agent_name,
                    prompt,
                    runtime_paths,
                    config,
                    session_id,
                    scope_context,
                    thread_history,
                    room_id,
                    knowledge,
                    include_interactive_questions=include_interactive_questions,
                    reply_to_event_id=reply_to_event_id,
                    active_event_ids=active_event_ids,
                    execution_identity=execution_identity,
                    compaction_outcomes_collector=compaction_outcomes_collector,
                    delegation_depth=delegation_depth,
                    system_enrichment_items=system_enrichment_items,
                    current_sender_id=_prompt_current_sender_id(
                        user_id,
                        include_openai_compat_guidance=include_openai_compat_guidance,
                    ),
                    include_openai_compat_guidance=include_openai_compat_guidance,
                    timing_scope=timing_scope,
                    model_prompt=model_prompt,
                )
                if pipeline_timing is not None:
                    pipeline_timing.mark("history_ready")
            except Exception as e:
                logger.exception("Error preparing agent", agent=agent_name)
                return get_user_friendly_error_message(e, agent_name)
            agent = prepared_run.agent
            run_input = prepared_run.run_input
            unseen_event_ids = prepared_run.unseen_event_ids
            if agent.model is not None:
                ai_runtime.install_queued_message_notice_hook(agent.model)

            metadata = build_matrix_run_metadata(
                reply_to_event_id,
                unseen_event_ids,
                extra_metadata=matrix_run_metadata,
            )
            if turn_recorder is not None:
                turn_recorder.set_run_metadata(metadata)

            response: RunOutput | None = None
            attempt_prompt = ai_runtime.copy_run_input(run_input)
            attempt_media_inputs = media_inputs

            try:
                for retried_without_inline_media in (False, True):
                    response = None
                    try:
                        if pipeline_timing is not None:
                            pipeline_timing.mark("model_request_sent", overwrite=True)
                        with bind_llm_request_log_context(
                            **_attempt_request_log_context(
                                session_id=session_id,
                                room_id=room_id,
                                thread_id=thread_id,
                                reply_to_event_id=reply_to_event_id,
                                prompt=prompt,
                                model_prompt=model_prompt,
                                attempt_prompt=attempt_prompt,
                                metadata=metadata,
                            ),
                        ):
                            response = await _run_cached_agent_attempt(
                                agent,
                                attempt_prompt,
                                session_id,
                                user_id=user_id,
                                run_id=attempt_run_id,
                                run_id_callback=run_id_callback,
                                media=attempt_media_inputs,
                                metadata=metadata,
                                timing_scope=timing_scope,
                            )
                    except Exception as e:
                        if not retried_without_inline_media and should_retry_without_inline_media(
                            e,
                            attempt_media_inputs,
                        ):
                            logger.warning(
                                "Retrying AI response without inline media after validation error",
                                agent=agent_name,
                                error=str(e),
                            )
                            attempt_prompt = ai_runtime.append_inline_media_fallback_to_run_input(run_input)
                            attempt_media_inputs = MediaInputs()
                            attempt_run_id = _next_retry_run_id(run_id)
                            continue

                        logger.exception("Error generating AI response", agent=agent_name)
                        return get_user_friendly_error_message(e, agent_name)

                    if response.status == RunStatus.error:
                        error_text = str(response.content or "Unknown agent error")
                        if not retried_without_inline_media and should_retry_without_inline_media(
                            error_text,
                            attempt_media_inputs,
                        ):
                            logger.warning(
                                "Retrying AI response without inline media after errored run output",
                                agent=agent_name,
                                error=error_text,
                            )
                            attempt_prompt = ai_runtime.append_inline_media_fallback_to_run_input(run_input)
                            attempt_media_inputs = MediaInputs()
                            attempt_run_id = _next_retry_run_id(run_id)
                            continue

                        logger.warning("AI response returned errored run output", agent=agent_name, error=error_text)

                    if response.status is not RunStatus.cancelled:
                        break

                assert response is not None
            finally:
                ai_runtime.cleanup_queued_notice_state(
                    run_output=response,
                    storage=scope_context.storage if scope_context is not None else None,
                    session_id=session_id,
                    session_type=SessionType.AGENT,
                    entity_name=agent_name,
                )

            if tool_trace_collector is not None:
                tool_trace_collector.extend(_extract_tool_trace(response))
            if run_metadata_collector is not None:
                run_metadata = _build_ai_run_metadata_content(
                    agent_name=agent_name,
                    config=config,
                    runtime_paths=runtime_paths,
                    run_id=response.run_id,
                    session_id=response.session_id or session_id,
                    status=response.status,
                    model=response.model,
                    model_provider=response.model_provider,
                    room_id=room_id,
                    metrics=response.metrics,
                    tool_count=len(response.tools) if response.tools is not None else 0,
                )
                if run_metadata:
                    run_metadata_collector.update(run_metadata)

            if response.status == RunStatus.cancelled:
                partial_text = _extract_interrupted_partial_text(
                    response.content,
                    messages=response.messages,
                )
                completed_tools, interrupted_tools = _extract_cancelled_tool_trace(response)
                if turn_recorder is not None:
                    turn_recorder.record_interrupted(
                        run_metadata=metadata,
                        assistant_text=partial_text,
                        completed_tools=completed_tools,
                        interrupted_tools=interrupted_tools,
                    )
                if turn_recorder is None:
                    persist_interrupted_replay(
                        scope_context=scope_context,
                        session_id=response.session_id or session_id,
                        run_id=response.run_id or attempt_run_id or str(uuid4()),
                        user_message=prompt,
                        partial_text=partial_text,
                        completed_tools=completed_tools,
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

            response_text = _extract_response_content(response, show_tool_calls=show_tool_calls)
            if turn_recorder is not None:
                turn_recorder.record_completed(
                    run_metadata=metadata,
                    assistant_text=_extract_replayable_response_text(response),
                    completed_tools=_extract_tool_trace(response),
                )
            return response_text
    except asyncio.CancelledError:
        if turn_recorder is not None:
            turn_recorder.record_interrupted(
                run_metadata=build_matrix_run_metadata(
                    reply_to_event_id,
                    unseen_event_ids,
                    extra_metadata=matrix_run_metadata,
                ),
                assistant_text=turn_recorder.assistant_text,
                completed_tools=turn_recorder.completed_tools,
                interrupted_tools=turn_recorder.interrupted_tools,
            )
        elif not standalone_interrupted_replay_persisted:
            persist_interrupted_replay(
                scope_context=scope_context,
                session_id=session_id,
                run_id=attempt_run_id or str(uuid4()),
                user_message=prompt,
                partial_text="",
                completed_tools=[],
                interrupted_tools=[],
                run_metadata=build_matrix_run_metadata(
                    reply_to_event_id,
                    unseen_event_ids,
                    extra_metadata=matrix_run_metadata,
                ),
                is_team=False,
            )
        raise
    finally:
        close_agent_runtime_sqlite_dbs(
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
    retried_without_inline_media: bool,
    timing_scope: str,
    state_updated: Callable[[], None] | None = None,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> AsyncGenerator[AIStreamChunk, None]:
    """Consume one streaming attempt, yielding chunks and mutating *state*."""
    del timing_scope
    try:
        async for event in stream_generator:
            if isinstance(event, RunContentEvent):
                if event.reasoning_content:
                    state.observed_reasoning_content = True
                if not event.content:
                    if event.reasoning_content:
                        yield event
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
                if event.reasoning_content:
                    state.observed_reasoning_content = True
                if event.content is not None:
                    final_text = str(event.content)
                    state.assistant_text = final_text
                    state.full_response = final_text
                    if state_updated is not None:
                        state_updated()
                    yield event
                    continue
                if event.reasoning_content:
                    yield event
                continue

            if isinstance(event, RunCancelledEvent):
                state.cancelled_run_event = event
                if state_updated is not None:
                    state_updated()
                return

            if isinstance(event, RunErrorEvent):
                error_text = event.content or "Unknown agent error"
                if _request_stream_retry(
                    state,
                    retried_without_inline_media=retried_without_inline_media,
                    media_inputs=media_inputs,
                    error=error_text,
                    log_message="Retrying streaming AI response without inline media after run error",
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
            retried_without_inline_media=retried_without_inline_media,
            media_inputs=media_inputs,
            error=e,
            log_message="Retrying streaming AI response without inline media after stream exception",
            agent_name=agent_name,
        ):
            return
        logger.exception("Error during streaming AI response")
        state.stream_exception = e


async def stream_agent_response(  # noqa: C901, PLR0912, PLR0915
    agent_name: str,
    prompt: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    model_prompt: str | None = None,
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
    active_event_ids: Collection[str] = frozenset(),
    show_tool_calls: bool = True,
    run_metadata_collector: dict[str, Any] | None = None,
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    delegation_depth: int = 0,
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
        model_prompt: Optional model-facing prompt after caller-side prompt shaping
        thread_id: Optional resolved Matrix thread target for the request
        room_id: Optional Matrix room ID for caller context
        knowledge: Optional shared knowledge base for RAG-enabled agents
        user_id: Matrix user ID of the sender, used by Agno's LearningMachine
        run_id: Explicit Agno run identifier used for graceful stop/cancel handling.
        run_id_callback: Optional callback that receives the active Agno run_id
            before each real streaming attempt starts.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        include_openai_compat_guidance: Whether to include OpenAI-compatible
            history-format guidance in the shared identity prompt.
        media: Optional multimodal inputs (audio/images/files/videos)
        reply_to_event_id: Matrix event ID of the triggering message, stored
            in run metadata for unseen message tracking and edit cleanup.
        active_event_ids: Live self-authored Matrix event IDs still tracked as
            actively streaming for this bot in the current room.
        show_tool_calls: Whether to include tool call details inline in the streamed response.
        run_metadata_collector: Optional mapping that receives versioned
            run/model/token metadata for Matrix message content.
        execution_identity: Request execution identity used to resolve scoped
            agent state, sessions, and memory consistently for this run.
        compaction_outcomes_collector: Optional list that receives completed
            compaction outcomes from auto-compaction and manual `compact_context`
            tool calls during this run.
        delegation_depth: Current nested delegation depth for delegated-agent runs.
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
    timing_scope = _build_timing_scope(
        reply_to_event_id=reply_to_event_id,
        run_id=run_id,
        session_id=session_id,
        agent_name=agent_name,
    )
    media_inputs = media or MediaInputs()
    agent: Agent | None = None
    scope_context: ScopeSessionContext | None = None
    standalone_interrupted_replay_persisted = False
    unseen_event_ids: list[str] = []
    attempt_run_id = run_id
    state = _StreamingAttemptState()

    try:
        try:
            _assert_agent_target(agent_name, config)
        except ValueError as e:
            yield get_user_friendly_error_message(e, agent_name)
            return
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
            try:
                if pipeline_timing is not None:
                    pipeline_timing.mark("ai_prepare_start")
                prepared_run = await _prepare_agent_and_prompt(
                    agent_name,
                    prompt,
                    runtime_paths,
                    config,
                    session_id,
                    scope_context,
                    thread_history,
                    room_id,
                    knowledge,
                    include_interactive_questions=include_interactive_questions,
                    reply_to_event_id=reply_to_event_id,
                    active_event_ids=active_event_ids,
                    execution_identity=execution_identity,
                    compaction_outcomes_collector=compaction_outcomes_collector,
                    delegation_depth=delegation_depth,
                    system_enrichment_items=system_enrichment_items,
                    current_sender_id=_prompt_current_sender_id(
                        user_id,
                        include_openai_compat_guidance=include_openai_compat_guidance,
                    ),
                    include_openai_compat_guidance=include_openai_compat_guidance,
                    timing_scope=timing_scope,
                    model_prompt=model_prompt,
                )
                if pipeline_timing is not None:
                    pipeline_timing.mark("history_ready")
            except Exception as e:
                logger.exception("Error preparing agent for streaming", agent=agent_name)
                yield get_user_friendly_error_message(e, agent_name)
                return
            agent = prepared_run.agent
            run_input = prepared_run.run_input
            unseen_event_ids = prepared_run.unseen_event_ids
            if agent.model is not None:
                ai_runtime.install_queued_message_notice_hook(agent.model)

            metadata = build_matrix_run_metadata(
                reply_to_event_id,
                unseen_event_ids,
                extra_metadata=matrix_run_metadata,
            )
            if turn_recorder is not None:
                turn_recorder.set_run_metadata(metadata)

            attempt_prompt = ai_runtime.copy_run_input(run_input)
            attempt_media_inputs = media_inputs
            state = _StreamingAttemptState()

            def _sync_live_turn_recorder() -> None:
                if turn_recorder is None:
                    return
                turn_recorder.sync_partial_state(
                    run_metadata=metadata,
                    assistant_text=state.assistant_text,
                    completed_tools=state.completed_tools,
                    interrupted_tools=[pending.trace_entry for pending in state.pending_tools],
                )

            try:
                for retried_without_inline_media in (False, True):
                    state = _StreamingAttemptState()

                    try:
                        if pipeline_timing is not None:
                            pipeline_timing.mark("model_request_sent", overwrite=True)
                        _note_attempt_run_id(run_id_callback, attempt_run_id)
                        request_context = _attempt_request_log_context(
                            session_id=session_id,
                            room_id=room_id,
                            thread_id=thread_id,
                            reply_to_event_id=reply_to_event_id,
                            prompt=prompt,
                            model_prompt=model_prompt,
                            attempt_prompt=attempt_prompt,
                            metadata=metadata,
                        )
                        with bind_llm_request_log_context(**request_context):
                            prepared_input = ai_runtime.attach_media_to_run_input(
                                attempt_prompt,
                                attempt_media_inputs,
                            )
                            stream_generator = agent.arun(
                                prepared_input,
                                session_id=session_id,
                                user_id=user_id,
                                run_id=attempt_run_id,
                                stream=True,
                                stream_events=True,
                                metadata=metadata,
                            )
                        stream_generator = _stream_with_request_log_context(
                            stream_generator,
                            request_context=request_context,
                        )
                        async for stream_chunk in _process_stream_events(
                            stream_generator,
                            state=state,
                            show_tool_calls=show_tool_calls,
                            agent_name=agent_name,
                            media_inputs=attempt_media_inputs,
                            retried_without_inline_media=retried_without_inline_media,
                            timing_scope=timing_scope,
                            state_updated=_sync_live_turn_recorder,
                            pipeline_timing=pipeline_timing,
                        ):
                            yield stream_chunk
                    except Exception as e:
                        if _request_stream_retry(
                            state,
                            retried_without_inline_media=retried_without_inline_media,
                            media_inputs=attempt_media_inputs,
                            error=e,
                            log_message="Retrying streaming AI response without inline media after validation error",
                            agent_name=agent_name,
                        ):
                            attempt_prompt = ai_runtime.append_inline_media_fallback_to_run_input(run_input)
                            attempt_media_inputs = MediaInputs()
                            attempt_run_id = _next_retry_run_id(run_id)
                            continue
                        logger.exception("Error starting streaming AI response")
                        yield get_user_friendly_error_message(e, agent_name)
                        return

                    if state.retry_requested:
                        attempt_prompt = ai_runtime.append_inline_media_fallback_to_run_input(run_input)
                        attempt_media_inputs = MediaInputs()
                        attempt_run_id = _next_retry_run_id(run_id)
                        continue

                    if state.user_error is not None:
                        yield get_user_friendly_error_message(state.user_error, agent_name)
                        return

                    if state.stream_exception is not None:
                        yield get_user_friendly_error_message(state.stream_exception, agent_name)
                        return

                    if state.cancelled_run_event is not None:
                        if turn_recorder is not None:
                            turn_recorder.record_interrupted(
                                run_metadata=metadata,
                                assistant_text=state.assistant_text,
                                completed_tools=state.completed_tools,
                                interrupted_tools=[pending.trace_entry for pending in state.pending_tools],
                            )
                        if run_metadata_collector is not None:
                            fallback_metrics = _build_model_request_metrics_fallback(
                                state.request_metric_totals,
                                state.first_token_latency,
                                state.observed_request_metric_keys,
                            )
                            cancelled_metadata = _build_ai_run_metadata_content(
                                agent_name=agent_name,
                                config=config,
                                runtime_paths=runtime_paths,
                                run_id=state.cancelled_run_event.run_id,
                                session_id=state.cancelled_run_event.session_id or session_id,
                                status=RunStatus.cancelled,
                                model=state.latest_model_id,
                                model_provider=state.latest_model_provider,
                                room_id=room_id,
                                metrics=fallback_metrics,
                                context_input_tokens=state.latest_request_input_tokens,
                                tool_count=state.observed_tool_calls,
                            )
                            if cancelled_metadata:
                                run_metadata_collector.update(cancelled_metadata)
                        if turn_recorder is None:
                            persist_interrupted_replay(
                                scope_context=scope_context,
                                session_id=state.cancelled_run_event.session_id or session_id,
                                run_id=state.cancelled_run_event.run_id or attempt_run_id or str(uuid4()),
                                user_message=prompt,
                                partial_text=state.assistant_text,
                                completed_tools=state.completed_tools,
                                interrupted_tools=[pending.trace_entry for pending in state.pending_tools],
                                run_metadata=metadata,
                                is_team=False,
                            )
                            standalone_interrupted_replay_persisted = True
                        _raise_agent_run_cancelled(state.cancelled_run_event.reason)

                    break

                if run_metadata_collector is not None:
                    fallback_metrics = _build_model_request_metrics_fallback(
                        state.request_metric_totals,
                        state.first_token_latency,
                        state.observed_request_metric_keys,
                    )
                    run_metadata = _build_ai_run_metadata_content(
                        agent_name=agent_name,
                        config=config,
                        runtime_paths=runtime_paths,
                        run_id=state.completed_run_event.run_id if state.completed_run_event is not None else None,
                        session_id=(
                            state.completed_run_event.session_id
                            if state.completed_run_event is not None
                            and state.completed_run_event.session_id is not None
                            else session_id
                        ),
                        status=RunStatus.completed,
                        model=state.latest_model_id,
                        model_provider=state.latest_model_provider,
                        room_id=room_id,
                        metrics=state.completed_run_event.metrics if state.completed_run_event is not None else None,
                        metrics_fallback=fallback_metrics,
                        context_input_tokens=state.latest_request_input_tokens,
                        tool_count=(
                            len(state.completed_run_event.tools)
                            if state.completed_run_event is not None and state.completed_run_event.tools is not None
                            else state.observed_tool_calls
                        ),
                    )
                    if run_metadata:
                        run_metadata_collector.update(run_metadata)
                if turn_recorder is not None:
                    turn_recorder.record_completed(
                        run_metadata=metadata,
                        assistant_text=state.assistant_text,
                        completed_tools=state.completed_tools,
                    )
            finally:
                ai_runtime.cleanup_queued_notice_state(
                    run_output=None,
                    storage=scope_context.storage if scope_context is not None else None,
                    session_id=session_id,
                    session_type=SessionType.AGENT,
                    entity_name=agent_name,
                )
    except asyncio.CancelledError:
        if turn_recorder is not None:
            turn_recorder.record_interrupted(
                run_metadata=build_matrix_run_metadata(
                    reply_to_event_id,
                    unseen_event_ids,
                    extra_metadata=matrix_run_metadata,
                ),
                assistant_text=state.assistant_text,
                completed_tools=state.completed_tools,
                interrupted_tools=[pending.trace_entry for pending in state.pending_tools],
            )
        elif not standalone_interrupted_replay_persisted:
            persist_interrupted_replay(
                scope_context=scope_context,
                session_id=session_id,
                run_id=attempt_run_id or str(uuid4()),
                user_message=prompt,
                partial_text=state.assistant_text,
                completed_tools=state.completed_tools,
                interrupted_tools=[pending.trace_entry for pending in state.pending_tools],
                run_metadata=build_matrix_run_metadata(
                    reply_to_event_id,
                    unseen_event_ids,
                    extra_metadata=matrix_run_metadata,
                ),
                is_team=False,
            )
        raise
    finally:
        close_agent_runtime_sqlite_dbs(
            agent,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )
