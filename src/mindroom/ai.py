"""AI integration module for MindRoom agents and memory management."""

from __future__ import annotations

import asyncio
import importlib
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from agno.db.base import SessionType
from agno.models.anthropic import Claude
from agno.models.cerebras import Cerebras
from agno.models.deepseek import DeepSeek
from agno.models.google import Gemini
from agno.models.groq import Groq
from agno.models.message import Message
from agno.models.metrics import Metrics
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.models.openrouter import OpenRouter
from agno.models.vertexai.claude import Claude as VertexAIClaude
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
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.agents import create_agent
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY,
    ROUTER_AGENT_NAME,
    RuntimePaths,
    runtime_env_path,
)
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.credentials_sync import get_api_key_for_provider, get_ollama_host
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.execution_preparation import (
    build_prompt_with_thread_history,
    prepare_agent_execution_context,
)
from mindroom.history import (
    CompactionOutcome,
    PreparedHistoryState,
)
from mindroom.history.compaction import compute_prompt_token_breakdown
from mindroom.history.runtime import (
    ScopeSessionContext,
    apply_replay_plan,
    close_agent_runtime_sqlite_dbs,
    open_resolved_scope_session_context,
)
from mindroom.history.types import HistoryScope
from mindroom.hooks import EnrichmentItem, render_system_enrichment_block
from mindroom.llm_request_logging import (
    bind_llm_request_logging_context,
    install_llm_request_logging_hooks,
    resolve_llm_request_log_dir,
)
from mindroom.logging_config import get_logger
from mindroom.media_fallback import append_inline_media_fallback_prompt, should_retry_without_inline_media
from mindroom.media_inputs import MediaInputs
from mindroom.memory import build_memory_enhanced_prompt
from mindroom.timing import DispatchPipelineTiming, timed
from mindroom.tool_system.events import (
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_combined,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Collection, Generator, Sequence

    from agno.agent import Agent
    from agno.db.sqlite import SqliteDb
    from agno.knowledge.knowledge import Knowledge
    from agno.models.base import Model

    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

__all__ = [
    "AIStreamChunk",
    "ai_response",
    "build_prompt_with_thread_history",
    "cleanup_queued_notice_state",
    "get_model_instance",
    "install_queued_message_notice_hook",
    "queued_message_signal_context",
    "scrub_queued_notice_session_context",
    "stream_agent_response",
]

AIStreamChunk = str | RunContentEvent | ToolCallStartedEvent | ToolCallCompletedEvent
_AI_RUN_METADATA_VERSION = 1
_QUEUED_MESSAGE_NOTICE_MARKER_KEY = "mindroom_queued_message_notice"
_QUEUED_MESSAGE_NOTICE_HOOK_ATTR = "_mindroom_queued_message_notice_hook_installed"
QUEUED_MESSAGE_NOTICE_TEXT = (
    "[SYSTEM NOTICE] A new message from the user has arrived in this thread while you were working. "
    "You should wrap up your current work and produce a final text response now. "
    "Avoid further tool calls unless strictly necessary. "
    "The new message will be handled in your next turn."
)


class _SupportsQueuedMessageState(Protocol):
    def has_pending_human_messages(self) -> bool: ...


@dataclass
class _QueuedMessageNoticeContext:
    state: _SupportsQueuedMessageState | None
    notice_delivered: bool = False


_queued_message_notice_context: ContextVar[_QueuedMessageNoticeContext | None] = ContextVar(
    "queued_message_notice_context",
    default=None,
)


@contextmanager
def queued_message_signal_context(
    signal: _SupportsQueuedMessageState | None,
) -> Generator[None, None, None]:
    """Bind one queued-message signal to the current async task."""
    token = _queued_message_notice_context.set(
        _QueuedMessageNoticeContext(state=signal),
    )
    try:
        yield
    finally:
        _queued_message_notice_context.reset(token)


def _has_queued_notice_marker(message: Message) -> bool:
    provider_data = message.provider_data
    return isinstance(provider_data, dict) and provider_data.get(_QUEUED_MESSAGE_NOTICE_MARKER_KEY) is True


def _is_queued_notice_message(message: Message) -> bool:
    """Return whether one Agno message is the hidden queued-message notice."""
    return _has_queued_notice_marker(message)


def _strip_queued_notice_messages(messages: list[Message] | None) -> bool:
    """Remove queued-message notices from one mutable message list."""
    if not messages:
        return False
    filtered_messages = [message for message in messages if not _is_queued_notice_message(message)]
    if len(filtered_messages) == len(messages):
        return False
    messages[:] = filtered_messages
    return True


def _cleanup_queued_notice_from_run_output(run_output: RunOutput | TeamRunOutput | None) -> bool:
    """Remove queued-message notices from one returned run output."""
    if run_output is None:
        return False
    changed = _strip_queued_notice_messages(run_output.messages)
    if isinstance(run_output, TeamRunOutput) and run_output.member_responses:
        for member_response in run_output.member_responses:
            if isinstance(member_response, RunOutput | TeamRunOutput):
                changed = _cleanup_queued_notice_from_run_output(member_response) or changed
    return changed


def _load_session_for_cleanup(
    raw_session: AgentSession | TeamSession | dict[str, object],
    *,
    session_type: SessionType,
) -> AgentSession | TeamSession | None:
    """Deserialize one stored Agno session for queued-notice cleanup."""
    if isinstance(raw_session, dict):
        session_mapping = cast("dict[str, Any]", raw_session)
        return (
            TeamSession.from_dict(session_mapping)
            if session_type is SessionType.TEAM
            else AgentSession.from_dict(session_mapping)
        )
    return raw_session


def _strip_queued_notice_from_session(
    session: AgentSession | TeamSession,
) -> bool:
    changed = False
    for run in session.runs or []:
        if isinstance(run, (RunOutput, TeamRunOutput)):
            changed = _cleanup_queued_notice_from_run_output(run) or changed
    return changed


def strip_queued_notice_from_session_storage(
    storage: SqliteDb,
    session_id: str,
    *,
    session_type: SessionType = SessionType.AGENT,
) -> bool:
    """Remove queued-message notices from one persisted Agno session."""
    raw_session = storage.get_session(session_id, session_type)
    if raw_session is None:
        return False
    session = _load_session_for_cleanup(
        cast("AgentSession | TeamSession | dict[str, object]", raw_session),
        session_type=session_type,
    )
    if session is None:
        return False
    changed = _strip_queued_notice_from_session(session)
    if changed:
        storage.upsert_session(session)
    return changed


def cleanup_queued_notice_state(
    *,
    run_output: RunOutput | TeamRunOutput | None,
    storage: SqliteDb | None,
    session_id: str | None,
    session_type: SessionType,
    entity_name: str,
) -> None:
    """Strip queued-message notices from returned and persisted run state."""
    _cleanup_queued_notice_from_run_output(run_output)
    if storage is None or not session_id:
        return
    try:
        strip_queued_notice_from_session_storage(
            storage,
            session_id,
            session_type=session_type,
        )
    except Exception:
        logger.exception(
            "Failed to strip queued-message notice from session history",
            entity=entity_name,
            session_id=session_id,
            session_type=session_type.value,
        )


def scrub_queued_notice_session_context(
    *,
    scope_context: ScopeSessionContext | None,
    entity_name: str,
) -> None:
    """Strip stale queued-message notices from the loaded session before replay."""
    if scope_context is None or scope_context.session is None:
        return
    try:
        if _strip_queued_notice_from_session(scope_context.session):
            scope_context.storage.upsert_session(scope_context.session)
    except Exception:
        logger.exception(
            "Failed to strip queued-message notice from loaded session history",
            entity=entity_name,
            session_id=scope_context.session.session_id,
            session_type="team" if isinstance(scope_context.session, TeamSession) else "agent",
        )


def install_queued_message_notice_hook(model: Model) -> None:
    """Append a hidden notice after tool results when a newer message is queued."""
    try:
        original_format_function_call_results = model.format_function_call_results
        model_dict = vars(model)
    except (AttributeError, TypeError):
        return
    if model_dict.get(_QUEUED_MESSAGE_NOTICE_HOOK_ATTR) is True:
        return
    setattr(model, _QUEUED_MESSAGE_NOTICE_HOOK_ATTR, True)

    def _format_function_call_results_with_notice(
        messages: list[Message],
        function_call_results: list[Message],
        compress_tool_results: bool = False,
        **kwargs: object,
    ) -> None:
        original_format_function_call_results(
            messages=messages,
            function_call_results=function_call_results,
            compress_tool_results=compress_tool_results,
            **kwargs,
        )
        if any(message.stop_after_tool_call for message in function_call_results):
            return
        notice_context = _queued_message_notice_context.get()
        if (
            notice_context is None
            or notice_context.notice_delivered
            or notice_context.state is None
            or not notice_context.state.has_pending_human_messages()
        ):
            return
        messages.append(
            Message(
                role="user",
                content=QUEUED_MESSAGE_NOTICE_TEXT,
                provider_data={_QUEUED_MESSAGE_NOTICE_MARKER_KEY: True},
            ),
        )
        notice_context.notice_delivered = True

    model_dict["format_function_call_results"] = _format_function_call_results_with_notice


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
    full_response: str = ""
    tool_count: int = 0
    observed_tool_calls: int = 0
    pending_tools: list[tuple[str, int]] = field(default_factory=list)
    latest_model_id: str | None = None
    latest_model_provider: str | None = None
    latest_request_input_tokens: int | None = None
    cancelled_run_event: RunCancelledEvent | None = None
    completed_run_event: RunCompletedEvent | None = None
    request_metric_totals: dict[str, int] = field(default_factory=_empty_request_metric_totals)
    first_token_latency: float | None = None
    first_token_logged: bool = False
    retry_requested: bool = False
    user_error: Exception | None = None
    stream_exception: Exception | None = None


def _canonical_provider(provider: str) -> str:
    """Return normalized provider key for model dispatch."""
    return provider.strip().lower().replace("-", "_")


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
        return _sanitize_metrics_payload(metrics_dict)
    if isinstance(metrics, dict):
        return _sanitize_metrics_payload(metrics)
    return None


def _build_model_request_metrics_fallback(
    totals: dict[str, int],
    first_token_latency: float | None,
) -> dict[str, Any] | None:
    payload = {key: value for key, value in totals.items() if value > 0}
    if payload.get("total_tokens") is None:
        input_tokens = payload.get("input_tokens")
        output_tokens = payload.get("output_tokens")
        if isinstance(input_tokens, int) and isinstance(output_tokens, int):
            payload["total_tokens"] = input_tokens + output_tokens
    if first_token_latency is not None:
        payload["time_to_first_token"] = format(first_token_latency, ".12g")
    return payload or None


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


def _create_model_for_provider(  # noqa: C901, PLR0912
    provider: str,
    model_id: str,
    model_config: ModelConfig,
    extra_kwargs: dict,
    runtime_paths: RuntimePaths,
) -> Model:
    """Create a model instance for a specific provider.

    Args:
        provider: The AI provider name
        model_id: The model identifier
        model_config: The model configuration object
        extra_kwargs: Additional keyword arguments for the model
        runtime_paths: Explicit runtime context for provider credentials and host resolution.

    Returns:
        Instantiated model for the provider

    Raises:
        ValueError: If provider not supported

    """
    canonical_provider = _canonical_provider(provider)

    if canonical_provider not in {"ollama", "vertexai_claude"} and "api_key" not in extra_kwargs:
        api_key = get_api_key_for_provider(canonical_provider, runtime_paths=runtime_paths)
        if api_key:
            extra_kwargs["api_key"] = api_key

    if canonical_provider == "vertexai_claude":
        if "project_id" not in extra_kwargs:
            project_id = runtime_paths.env_value("ANTHROPIC_VERTEX_PROJECT_ID")
            if project_id:
                extra_kwargs["project_id"] = project_id
        if "region" not in extra_kwargs:
            region = runtime_paths.env_value("CLOUD_ML_REGION")
            if region:
                extra_kwargs["region"] = region
        if "base_url" not in extra_kwargs:
            base_url = runtime_paths.env_value("ANTHROPIC_VERTEX_BASE_URL")
            if base_url:
                extra_kwargs["base_url"] = base_url
        client_params = dict(cast("dict[str, Any]", extra_kwargs.get("client_params") or {}))
        if "credentials" not in client_params and (
            google_application_credentials := runtime_env_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
        ):
            google_auth = importlib.import_module("google.auth")
            load_credentials_from_file = google_auth.load_credentials_from_file
            credentials, _project_id = load_credentials_from_file(
                str(google_application_credentials),
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            client_params["credentials"] = credentials
        if client_params:
            extra_kwargs["client_params"] = client_params

    # Handle Ollama separately due to special host configuration
    if canonical_provider == "ollama":
        # Priority: model config > env/CredentialsManager > default
        # This allows per-model host configuration in config.yaml
        host = model_config.host or get_ollama_host(runtime_paths=runtime_paths) or "http://localhost:11434"
        logger.debug(f"Using Ollama host: {host}")
        return Ollama(id=model_id, host=host, **extra_kwargs)

    # Handle OpenRouter separately due to API key capture timing issue
    if canonical_provider == "openrouter":
        # OpenRouter needs the API key passed explicitly because it captures
        # the environment variable at import time, not at instantiation time
        api_key = extra_kwargs.pop("api_key", None)
        if not api_key:
            api_key = get_api_key_for_provider(canonical_provider, runtime_paths=runtime_paths)
        if not api_key:
            logger.warning("No OpenRouter API key found in environment or CredentialsManager")
        return OpenRouter(id=model_id, api_key=api_key, **extra_kwargs)

    # Map providers to their model classes for simple instantiation
    provider_map: dict[str, type[Model]] = {
        "openai": OpenAIChat,
        "anthropic": Claude,
        "gemini": Gemini,
        "google": Gemini,
        "vertexai_claude": VertexAIClaude,
        "cerebras": Cerebras,
        "groq": Groq,
        "deepseek": DeepSeek,
    }

    model_class = provider_map.get(canonical_provider)
    if model_class is not None:
        return model_class(id=model_id, **extra_kwargs)

    msg = f"Unsupported AI provider: {provider}"
    raise ValueError(msg)


def get_model_instance(
    config: Config,
    runtime_paths: RuntimePaths,
    model_name: str = "default",
) -> Model:
    """Get a model instance from config.yaml.

    Args:
        config: Application configuration
        runtime_paths: Explicit runtime context for model credentials and env-backed settings.
        model_name: Name of the model configuration to use (default: "default")

    Returns:
        Instantiated model

    Raises:
        ValueError: If model not found or provider not supported

    """
    if model_name not in config.models:
        available = ", ".join(sorted(config.models.keys()))
        msg = f"Unknown model: {model_name}. Available models: {available}"
        raise ValueError(msg)

    model_config = config.models[model_name]
    provider = model_config.provider
    model_id = model_config.id

    logger.info("Using AI model", model=model_name, provider=provider, id=model_id)

    # Get extra kwargs if specified
    extra_kwargs = dict(model_config.extra_kwargs or {})

    # Check for model-specific API key first, then fall back to provider-level
    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)
    model_creds = creds_manager.load_credentials(f"model:{model_name}")
    model_api_key = model_creds.get("api_key") if model_creds else None

    if model_api_key:
        extra_kwargs["api_key"] = model_api_key

    return _create_model_for_provider(
        provider,
        model_id,
        model_config,
        extra_kwargs,
        runtime_paths,
    )


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
    if retried_without_inline_media or state.full_response:
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
    if not show_tool_calls:
        return

    state.tool_count += 1
    tool_msg, trace_entry = format_tool_started_event(event.tool, tool_index=state.tool_count)
    if tool_msg:
        state.full_response += tool_msg
    if trace_entry is not None:
        state.pending_tools.append((trace_entry.tool_name, state.tool_count))


def _track_stream_tool_completed(
    state: _StreamingAttemptState,
    event: ToolCallCompletedEvent,
    *,
    show_tool_calls: bool,
    agent_name: str,
) -> None:
    """Track completed tool-call metadata for streaming output."""
    if not show_tool_calls:
        return

    info = extract_tool_completed_info(event.tool)
    if info is None:
        return
    tool_name, result = info
    match_pos = next(
        (pos for pos in range(len(state.pending_tools) - 1, -1, -1) if state.pending_tools[pos][0] == tool_name),
        None,
    )
    if match_pos is None:
        logger.warning(
            "Missing pending tool start in AI stream; skipping completion marker",
            tool_name=tool_name,
            agent=agent_name,
        )
        return
    _, tool_index = state.pending_tools.pop(match_pos)
    state.full_response, _ = complete_pending_tool_block(
        state.full_response,
        tool_name,
        result,
        tool_index=tool_index,
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
    if isinstance(event.output_tokens, int):
        state.request_metric_totals["output_tokens"] += event.output_tokens
    if isinstance(event.total_tokens, int):
        state.request_metric_totals["total_tokens"] += event.total_tokens
    if isinstance(event.reasoning_tokens, int):
        state.request_metric_totals["reasoning_tokens"] += event.reasoning_tokens
    if isinstance(event.cache_read_tokens, int):
        state.request_metric_totals["cache_read_tokens"] += event.cache_read_tokens
    if isinstance(event.cache_write_tokens, int):
        state.request_metric_totals["cache_write_tokens"] += event.cache_write_tokens
    if state.first_token_latency is None and isinstance(event.time_to_first_token, (int, float)):
        state.first_token_latency = float(event.time_to_first_token)


async def cached_agent_run(
    agent: Agent,
    full_prompt: str,
    session_id: str,
    *,
    user_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    media: MediaInputs | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunOutput:
    """Shared wrapper for one `agent.arun()` call."""
    media_inputs = media or MediaInputs()
    _note_attempt_run_id(run_id_callback, run_id)
    return await agent.arun(
        full_prompt,
        session_id=session_id,
        user_id=user_id,
        run_id=run_id,
        audio=media_inputs.audio,
        images=media_inputs.images,
        files=media_inputs.files,
        videos=media_inputs.videos,
        metadata=metadata,
    )


@timed("model_request_to_completion")
async def _run_cached_agent_attempt(
    agent: Agent,
    full_prompt: str,
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
    return await cached_agent_run(
        agent,
        full_prompt,
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
    timing_scope: str | None = None,
) -> tuple[Agent, str, list[str], PreparedHistoryState]:
    """Prepare agent and full prompt for AI processing.

    Returns:
        Tuple of (agent, full_prompt, unseen_event_ids, prepared_history).
        unseen_event_ids is the list of event_ids injected as unseen context
        (empty when using the fallback path).

    """
    _assert_agent_target(agent_name, config)
    storage_path = runtime_paths.storage_root
    enhanced_prompt = await build_memory_enhanced_prompt(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        timing_scope=timing_scope,
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
        execution_identity=execution_identity,
        delegation_depth=delegation_depth,
        timing_scope=timing_scope,
    )
    if system_enrichment_items:
        agent.additional_context = _render_system_enrichment_context(
            system_enrichment_items,
            timing_scope=timing_scope,
        )

    prepared_execution = await prepare_agent_execution_context(
        scope_context=scope_context,
        agent=agent,
        agent_name=agent_name,
        prompt=enhanced_prompt,
        thread_history=thread_history,
        runtime_paths=runtime_paths,
        config=config,
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        active_event_ids=active_event_ids,
        compaction_outcomes_collector=compaction_outcomes_collector,
        timing_scope=timing_scope,
    )
    prepared_history = PreparedHistoryState(
        compaction_outcomes=prepared_execution.compaction_outcomes,
        replay_plan=prepared_execution.replay_plan,
        replays_persisted_history=prepared_execution.replays_persisted_history,
    )
    if prepared_execution.replay_plan is not None:
        apply_replay_plan(target=agent, replay_plan=prepared_execution.replay_plan)
    full_prompt = prepared_execution.final_prompt
    unseen_event_ids = prepared_execution.unseen_event_ids

    if prepared_history.compaction_outcomes:
        breakdown = _compute_compaction_token_breakdown(
            agent,
            full_prompt,
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

    logger.info("Preparing agent and prompt", agent=agent_name, full_prompt=full_prompt)
    return agent, full_prompt, unseen_event_ids, prepared_history


async def ai_response(  # noqa: C901, PLR0912, PLR0915
    agent_name: str,
    prompt: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    thread_history: Sequence[ResolvedVisibleMessage] | None = None,
    room_id: str | None = None,
    thread_id: str | None = None,
    knowledge: Knowledge | None = None,
    user_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    include_interactive_questions: bool = True,
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
        room_id: Optional Matrix room ID for caller context
        thread_id: Optional Matrix thread root event ID for caller context
        knowledge: Optional shared knowledge base for RAG-enabled agents
        user_id: Matrix user ID of the sender, used by Agno's LearningMachine
        run_id: Explicit Agno run identifier used for graceful stop/cancel handling.
        run_id_callback: Optional callback that receives the active Agno run_id
            before each real run attempt starts.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
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
    runtime_model = config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    )
    media_inputs = media or MediaInputs()
    agent: Agent | None = None
    scope_context: ScopeSessionContext | None = None
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
            scrub_queued_notice_session_context(
                scope_context=scope_context,
                entity_name=agent_name,
            )
            try:
                if pipeline_timing is not None:
                    pipeline_timing.mark("ai_prepare_start")
                agent, full_prompt, unseen_event_ids, _prepared_history = await _prepare_agent_and_prompt(
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
                    timing_scope=timing_scope,
                )
                if pipeline_timing is not None:
                    pipeline_timing.mark("history_ready")
            except Exception as e:
                logger.exception("Error preparing agent", agent=agent_name)
                return get_user_friendly_error_message(e, agent_name)
            if agent.model is not None:
                install_queued_message_notice_hook(agent.model)
                if config.debug.log_llm_requests:
                    install_llm_request_logging_hooks(
                        agent.model,
                        log_dir=resolve_llm_request_log_dir(
                            runtime_paths=runtime_paths,
                            configured_log_dir=config.debug.llm_request_log_dir,
                        ),
                        default_agent_name=agent_name,
                        default_model_config_name=runtime_model.model_name,
                    )

            metadata = build_matrix_run_metadata(
                reply_to_event_id,
                unseen_event_ids,
                extra_metadata=matrix_run_metadata,
            )

            response: RunOutput | None = None
            attempt_prompt = full_prompt
            attempt_media_inputs = media_inputs
            attempt_run_id = run_id

            try:
                for retried_without_inline_media in (False, True):
                    response = None
                    try:
                        if pipeline_timing is not None:
                            pipeline_timing.mark("model_request_sent", overwrite=True)
                        with bind_llm_request_logging_context(
                            session_id=session_id,
                            room_id=room_id,
                            thread_id=thread_id,
                            run_id=attempt_run_id,
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
                            attempt_prompt = append_inline_media_fallback_prompt(full_prompt)
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
                            attempt_prompt = append_inline_media_fallback_prompt(full_prompt)
                            attempt_media_inputs = MediaInputs()
                            attempt_run_id = _next_retry_run_id(run_id)
                            continue

                        logger.warning("AI response returned errored run output", agent=agent_name, error=error_text)

                    if response.status is not RunStatus.cancelled:
                        break

                assert response is not None
            finally:
                cleanup_queued_notice_state(
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
                raise asyncio.CancelledError(response.content or "Run cancelled")
            if response.status == RunStatus.error:
                return get_user_friendly_error_message(
                    Exception(str(response.content or "Unknown agent error")),
                    agent_name,
                )

            return _extract_response_content(response, show_tool_calls=show_tool_calls)
    finally:
        close_agent_runtime_sqlite_dbs(
            agent,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )


@timed("model_request_to_completion")
async def _process_stream_events(  # noqa: C901, PLR0912
    stream_generator: AsyncIterator[object],
    *,
    state: _StreamingAttemptState,
    show_tool_calls: bool,
    agent_name: str,
    media_inputs: MediaInputs,
    retried_without_inline_media: bool,
    timing_scope: str,
    request_started_at: float,
    pipeline_timing: DispatchPipelineTiming | None = None,
) -> AsyncGenerator[AIStreamChunk, None]:
    """Consume one streaming attempt, yielding chunks and mutating *state*."""
    try:
        async for event in stream_generator:
            if isinstance(event, RunContentEvent) and event.content:
                if not state.first_token_logged:
                    state.first_token_logged = True
                    if pipeline_timing is not None:
                        pipeline_timing.mark("model_first_token")
                    if os.environ.get("MINDROOM_TIMING") == "1":
                        elapsed_seconds = time.monotonic() - request_started_at
                        prefix = f"[{timing_scope}] " if timing_scope else ""
                        logger.info(
                            f"TIMING {prefix}model_request_to_first_token: {elapsed_seconds:.3f}s",
                            timing_scope=timing_scope,
                            timing_step="model_request_to_first_token",
                            elapsed_s=round(elapsed_seconds, 3),
                            retried_without_inline_media=retried_without_inline_media,
                            agent=agent_name,
                        )
                chunk_text = str(event.content)
                state.full_response += chunk_text
                yield event
                continue

            if isinstance(event, ToolCallStartedEvent):
                _track_stream_tool_started(
                    state,
                    event,
                    show_tool_calls=show_tool_calls,
                )
                yield event
                continue

            if isinstance(event, ToolCallCompletedEvent):
                _track_stream_tool_completed(
                    state,
                    event,
                    show_tool_calls=show_tool_calls,
                    agent_name=agent_name,
                )
                yield event
                continue

            if isinstance(event, ModelRequestCompletedEvent):
                _track_model_request_metrics(state, event)
                continue

            if isinstance(event, RunCompletedEvent):
                state.completed_run_event = event
                continue

            if isinstance(event, RunCancelledEvent):
                state.cancelled_run_event = event
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
    room_id: str | None = None,
    thread_id: str | None = None,
    knowledge: Knowledge | None = None,
    user_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    include_interactive_questions: bool = True,
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
        room_id: Optional Matrix room ID for caller context
        thread_id: Optional Matrix thread root event ID for caller context
        knowledge: Optional shared knowledge base for RAG-enabled agents
        user_id: Matrix user ID of the sender, used by Agno's LearningMachine
        run_id: Explicit Agno run identifier used for graceful stop/cancel handling.
        run_id_callback: Optional callback that receives the active Agno run_id
            before each real streaming attempt starts.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
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
    runtime_model = config.resolve_runtime_model(
        entity_name=agent_name,
        room_id=room_id,
        runtime_paths=runtime_paths,
    )
    media_inputs = media or MediaInputs()
    agent: Agent | None = None
    scope_context: ScopeSessionContext | None = None

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
            scrub_queued_notice_session_context(
                scope_context=scope_context,
                entity_name=agent_name,
            )
            try:
                if pipeline_timing is not None:
                    pipeline_timing.mark("ai_prepare_start")
                agent, full_prompt, unseen_event_ids, _prepared_history = await _prepare_agent_and_prompt(
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
                    timing_scope=timing_scope,
                )
                if pipeline_timing is not None:
                    pipeline_timing.mark("history_ready")
            except Exception as e:
                logger.exception("Error preparing agent for streaming", agent=agent_name)
                yield get_user_friendly_error_message(e, agent_name)
                return
            if agent.model is not None:
                install_queued_message_notice_hook(agent.model)
                if config.debug.log_llm_requests:
                    install_llm_request_logging_hooks(
                        agent.model,
                        log_dir=resolve_llm_request_log_dir(
                            runtime_paths=runtime_paths,
                            configured_log_dir=config.debug.llm_request_log_dir,
                        ),
                        default_agent_name=agent_name,
                        default_model_config_name=runtime_model.model_name,
                    )

            metadata = build_matrix_run_metadata(
                reply_to_event_id,
                unseen_event_ids,
                extra_metadata=matrix_run_metadata,
            )

            attempt_prompt = full_prompt
            attempt_media_inputs = media_inputs
            attempt_run_id = run_id
            state = _StreamingAttemptState()

            try:
                for retried_without_inline_media in (False, True):
                    state = _StreamingAttemptState()

                    try:
                        request_started_at = time.monotonic()
                        if pipeline_timing is not None:
                            pipeline_timing.mark("model_request_sent", overwrite=True)
                        _note_attempt_run_id(run_id_callback, attempt_run_id)
                        with bind_llm_request_logging_context(
                            session_id=session_id,
                            room_id=room_id,
                            thread_id=thread_id,
                            run_id=attempt_run_id,
                        ):
                            stream_generator = agent.arun(
                                attempt_prompt,
                                session_id=session_id,
                                user_id=user_id,
                                run_id=attempt_run_id,
                                audio=attempt_media_inputs.audio,
                                images=attempt_media_inputs.images,
                                files=attempt_media_inputs.files,
                                videos=attempt_media_inputs.videos,
                                stream=True,
                                stream_events=True,
                                metadata=metadata,
                            )
                            async for stream_chunk in _process_stream_events(
                                stream_generator,
                                state=state,
                                show_tool_calls=show_tool_calls,
                                agent_name=agent_name,
                                media_inputs=attempt_media_inputs,
                                retried_without_inline_media=retried_without_inline_media,
                                timing_scope=timing_scope,
                                request_started_at=request_started_at,
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
                            attempt_prompt = append_inline_media_fallback_prompt(full_prompt)
                            attempt_media_inputs = MediaInputs()
                            attempt_run_id = _next_retry_run_id(run_id)
                            continue
                        logger.exception("Error starting streaming AI response")
                        yield get_user_friendly_error_message(e, agent_name)
                        return

                    if state.retry_requested:
                        attempt_prompt = append_inline_media_fallback_prompt(full_prompt)
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
                        if run_metadata_collector is not None:
                            fallback_metrics = _build_model_request_metrics_fallback(
                                state.request_metric_totals,
                                state.first_token_latency,
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
                        raise asyncio.CancelledError(state.cancelled_run_event.reason or "Run cancelled")

                    break

                if run_metadata_collector is not None:
                    fallback_metrics = _build_model_request_metrics_fallback(
                        state.request_metric_totals,
                        state.first_token_latency,
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
            finally:
                cleanup_queued_notice_state(
                    run_output=None,
                    storage=scope_context.storage if scope_context is not None else None,
                    session_id=session_id,
                    session_type=SessionType.AGENT,
                    entity_name=agent_name,
                )
    finally:
        close_agent_runtime_sqlite_dbs(
            agent,
            shared_scope_storage=scope_context.storage if scope_context is not None else None,
        )
