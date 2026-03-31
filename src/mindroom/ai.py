"""AI integration module for MindRoom agents and memory management."""

from __future__ import annotations

import asyncio
import functools
import importlib
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import diskcache
from agno.models.anthropic import Claude
from agno.models.cerebras import Cerebras
from agno.models.deepseek import DeepSeek
from agno.models.google import Gemini
from agno.models.groq import Groq
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

from mindroom.agents import create_agent, create_session_storage, get_agent_session
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    COMPACTION_NOTICE_CONTENT_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    RuntimePaths,
    runtime_ai_cache_enabled,
    runtime_env_path,
)
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.credentials_sync import get_api_key_for_provider, get_ollama_host
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.history import (
    CompactionOutcome,
    PreparedHistoryState,
    prepare_history_for_run,
)
from mindroom.history.runtime import estimate_preparation_static_tokens, resolve_history_scope
from mindroom.history.storage import read_scope_seen_event_ids
from mindroom.logging_config import get_logger
from mindroom.media_fallback import append_inline_media_fallback_prompt, should_retry_without_inline_media
from mindroom.media_inputs import MediaInputs
from mindroom.memory import build_memory_enhanced_prompt
from mindroom.streaming import clean_partial_reply_text, is_in_progress_message, is_interrupted_partial_reply
from mindroom.tool_system.events import (
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_combined,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Collection
    from pathlib import Path

    from agno.agent import Agent
    from agno.knowledge.knowledge import Knowledge
    from agno.models.base import Model

    from mindroom.config.main import Config
    from mindroom.config.models import ModelConfig
    from mindroom.tool_system.events import ToolTraceEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

AIStreamChunk = str | RunContentEvent | ToolCallStartedEvent | ToolCallCompletedEvent
_AI_RUN_METADATA_VERSION = 1
_DEFAULT_UNSEEN_MESSAGES_HEADER = "Messages from other participants since your last response:"
_INTERRUPTED_PARTIAL_REPLY_HEADER = (
    "Messages since your last response:\n"
    "Your previous response was interrupted before completion. "
    "The partial content below may be incomplete. Continue from where you left off if appropriate."
)
_IN_PROGRESS_PARTIAL_REPLY_HEADER = (
    "Messages since your last response:\n"
    "Your previous response is still being delivered. Do NOT repeat or redo that work. "
    "The partial content is shown below for context only."
)
_MIXED_PARTIAL_REPLY_HEADER = (
    "Messages since your last response:\n"
    "Some partial content from your previous response is still being delivered, so do NOT repeat or redo that work. "
    "Other partial content was interrupted before completion and may be incomplete. "
    "Continue from where you left off if appropriate."
)
_PARTIAL_REPLY_SENDER_LABELS = {
    "interrupted": "You (interrupted reply draft)",
    "in_progress": "You (reply still streaming)",
}


class _PartialReplyKind(str, Enum):
    """Classification for a self-authored partial reply preserved in prompt context."""

    IN_PROGRESS = "in_progress"
    INTERRUPTED = "interrupted"


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


def _note_attempt_run_id(run_id_callback: Callable[[str], None] | None, run_id: str | None) -> None:
    """Publish the current run_id before starting a real Agno run attempt."""
    if run_id_callback is not None and run_id is not None:
        run_id_callback(run_id)


@dataclass
class _StreamingAttemptState:
    full_response: str = ""
    tool_count: int = 0
    observed_tool_calls: int = 0
    pending_tools: list[tuple[str, int]] = field(default_factory=list)
    latest_model_id: str | None = None
    latest_model_provider: str | None = None
    cancelled_run_event: RunCancelledEvent | None = None
    completed_run_event: RunCompletedEvent | None = None
    request_metric_totals: dict[str, int] = field(default_factory=_empty_request_metric_totals)
    first_token_latency: float | None = None
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


def _get_model_config(config: Config, agent_name: str) -> tuple[str | None, ModelConfig | None]:
    """Return configured model name/config for an agent when available."""
    if agent_name not in config.agents and agent_name not in config.teams and agent_name != ROUTER_AGENT_NAME:
        return None, None
    model_name = config.get_entity_model_name(agent_name)
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
    input_tokens: int | None,
    model_config: ModelConfig | None,
) -> dict[str, Any] | None:
    if input_tokens is None or model_config is None or model_config.context_window is None:
        return None
    context_window = model_config.context_window
    if context_window <= 0:
        return None
    return {
        "input_tokens": input_tokens,
        "window_tokens": context_window,
    }


def _build_ai_run_metadata_content(  # noqa: C901, PLR0912
    *,
    agent_name: str,
    config: Config,
    run_id: str | None,
    session_id: str | None,
    status: RunStatus | str | None,
    model: str | None,
    model_provider: str | None,
    metrics: Metrics | dict[str, Any] | None = None,
    metrics_fallback: dict[str, Any] | None = None,
    tool_count: int | None = None,
) -> dict[str, Any] | None:
    model_name, model_config = _get_model_config(config, agent_name)
    model_id = model or (model_config.id if model_config is not None else None)
    provider = model_provider or (model_config.provider if model_config is not None else None)

    usage_payload = _serialize_metrics(metrics)
    if usage_payload is None and metrics_fallback:
        usage_payload = dict(metrics_fallback)

    input_tokens = usage_payload.get("input_tokens") if usage_payload else None
    if not isinstance(input_tokens, int):
        input_tokens = None

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
        input_tokens=input_tokens,
        model_config=model_config,
    )
    if context_payload:
        payload["context"] = context_payload
    if tool_count is not None:
        payload["tools"] = {"count": tool_count}

    if len(payload) == 1:
        return None
    return {AI_RUN_METADATA_KEY: payload}


@functools.cache
def _get_cache(storage_path: Path, enabled: bool) -> diskcache.Cache | None:
    """Get or create a cache instance for the given storage path."""
    return diskcache.Cache(storage_path / ".ai_cache") if enabled else None


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


def build_prompt_with_thread_history(
    prompt: str,
    thread_history: list[dict[str, Any]] | None = None,
    *,
    header: str = "Previous conversation in this thread:",
    prompt_intro: str = "Current message:\n",
    max_messages: int | None = None,
    max_message_length: int | None = None,
    missing_sender_label: str | None = None,
) -> str:
    """Build a prompt with thread history context when available."""
    if not thread_history:
        return prompt
    messages = thread_history[-max_messages:] if max_messages is not None else thread_history
    context_lines: list[str] = []
    for msg in messages:
        body = msg.get("body")
        if not isinstance(body, str) or not body:
            continue
        if max_message_length is not None and len(body) >= max_message_length:
            continue
        sender = msg.get("sender")
        if not isinstance(sender, str) or not sender:
            if missing_sender_label is None:
                continue
            sender = missing_sender_label
        context_lines.append(f"{sender}: {body}")
    if not context_lines:
        return prompt
    context = "\n".join(context_lines)
    return f"{header}\n{context}\n\n{prompt_intro}{prompt}"


def _classify_partial_reply(
    msg: dict[str, Any],
    *,
    active_event_ids: Collection[str],
) -> _PartialReplyKind | None:
    """Classify a self-authored partial reply from persisted stream metadata first."""
    status = msg.get("stream_status")
    if status == STREAM_STATUS_COMPLETED:
        return None

    partial_kind: _PartialReplyKind | None = None
    if status in {STREAM_STATUS_CANCELLED, STREAM_STATUS_ERROR}:
        partial_kind = _PartialReplyKind.INTERRUPTED
    elif status in {STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING}:
        event_id = msg.get("event_id")
        if isinstance(event_id, str):
            return _PartialReplyKind.IN_PROGRESS if event_id in active_event_ids else _PartialReplyKind.INTERRUPTED
        partial_kind = _PartialReplyKind.IN_PROGRESS
    else:
        body = msg.get("body", "")
        if not isinstance(body, str):
            return None
        if is_interrupted_partial_reply(body):
            partial_kind = _PartialReplyKind.INTERRUPTED
        elif is_in_progress_message(body):
            partial_kind = _PartialReplyKind.IN_PROGRESS

    return partial_kind


def _clean_partial_reply_body(body: str) -> str:
    """Strip streaming markers and status notes from partial reply text."""
    return clean_partial_reply_text(body)


def _build_unseen_messages_header(partial_reply_kinds: set[_PartialReplyKind]) -> str:
    """Choose the unseen-context header for the partial-reply mix present."""
    if not partial_reply_kinds:
        return _DEFAULT_UNSEEN_MESSAGES_HEADER
    if partial_reply_kinds == {_PartialReplyKind.INTERRUPTED}:
        return _INTERRUPTED_PARTIAL_REPLY_HEADER
    if partial_reply_kinds == {_PartialReplyKind.IN_PROGRESS}:
        return _IN_PROGRESS_PARTIAL_REPLY_HEADER
    return _MIXED_PARTIAL_REPLY_HEADER


def _get_unseen_event_ids_for_metadata(unseen_messages: list[dict[str, Any]]) -> list[str]:
    """Return unseen event IDs that should be persisted as consumed by this run."""
    event_ids: list[str] = []
    for msg in unseen_messages:
        event_id = msg.get("event_id")
        if not isinstance(event_id, str):
            continue
        if msg.get("partial_reply_kind") is _PartialReplyKind.IN_PROGRESS:
            continue
        event_ids.append(event_id)
    return event_ids


def _get_unseen_messages(
    thread_history: list[dict[str, Any]],
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    seen_event_ids: set[str],
    current_event_id: str | None,
    *,
    active_event_ids: Collection[str],
) -> tuple[list[dict[str, Any]], set[_PartialReplyKind]]:
    """Filter thread_history to messages not yet consumed by this agent.

    Excludes:
    - Messages whose event_id is in seen_event_ids
    - The current triggering message (current_event_id)

    Includes self-authored partial replies with cleaned body text and a
    per-message classification that distinguishes interrupted drafts from
    still-active in-progress replies.
    """
    matrix_id = config.get_ids(runtime_paths).get(agent_name)
    return _get_unseen_messages_for_sender(
        thread_history,
        sender_id=matrix_id.full_id if matrix_id else None,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
    )


def _get_unseen_messages_for_sender(
    thread_history: list[dict[str, Any]],
    *,
    sender_id: str | None,
    seen_event_ids: set[str],
    current_event_id: str | None,
    active_event_ids: Collection[str],
) -> tuple[list[dict[str, Any]], set[_PartialReplyKind]]:
    """Filter thread_history to unseen messages for one Matrix sender."""
    unseen: list[dict[str, Any]] = []
    partial_reply_kinds: set[_PartialReplyKind] = set()
    for msg in thread_history:
        event_id = msg.get("event_id")
        sender = msg.get("sender")
        content = msg.get("content")
        # Skip already-seen messages
        if event_id and event_id in seen_event_ids:
            continue
        # Skip the current triggering message
        if current_event_id and event_id == current_event_id:
            continue
        if isinstance(content, dict) and COMPACTION_NOTICE_CONTENT_KEY in content:
            continue
        if sender_id and sender == sender_id:
            partial_kind = _classify_partial_reply(
                msg,
                active_event_ids=active_event_ids,
            )
            if partial_kind is None:
                continue
            body = msg.get("body")
            if not isinstance(body, str):
                continue
            cleaned_body = _clean_partial_reply_body(body)
            if not cleaned_body:
                continue
            partial_reply_kinds.add(partial_kind)
            unseen.append(
                {
                    **msg,
                    "sender": _PARTIAL_REPLY_SENDER_LABELS.get(partial_kind.value, "You (partial reply)"),
                    "body": cleaned_body,
                    "partial_reply_kind": partial_kind,
                },
            )
            continue
        unseen.append(msg)
    return unseen, partial_reply_kinds


def build_prompt_with_unseen_thread_context(
    prompt: str,
    thread_history: list[dict[str, Any]] | None,
    *,
    seen_event_ids: set[str],
    current_event_id: str | None,
    active_event_ids: Collection[str],
    response_sender_id: str | None,
) -> tuple[str, list[str]]:
    """Prepend unseen thread messages and return their persisted event ids."""
    if not current_event_id or not thread_history:
        return prompt, []

    unseen_messages, partial_reply_kinds = _get_unseen_messages_for_sender(
        thread_history,
        sender_id=response_sender_id,
        seen_event_ids=seen_event_ids,
        current_event_id=current_event_id,
        active_event_ids=active_event_ids,
    )
    prompt_with_unseen = _build_prompt_with_unseen(
        prompt,
        unseen_messages,
        partial_reply_kinds=partial_reply_kinds,
    )
    return prompt_with_unseen, _get_unseen_event_ids_for_metadata(unseen_messages)


def _build_prompt_with_unseen(
    prompt: str,
    unseen_messages: list[dict[str, Any]],
    *,
    partial_reply_kinds: set[_PartialReplyKind] | None,
) -> str:
    """Prepend unseen messages from other participants to the prompt."""
    if not unseen_messages:
        return prompt
    return build_prompt_with_thread_history(
        prompt,
        unseen_messages,
        header=_build_unseen_messages_header(partial_reply_kinds or set()),
    )


def build_matrix_run_metadata(reply_to_event_id: str | None, unseen_event_ids: list[str]) -> dict[str, Any] | None:
    """Build metadata dict for a run, tracking consumed Matrix event ids."""
    if not reply_to_event_id:
        return None
    return {
        "matrix_event_id": reply_to_event_id,
        "matrix_seen_event_ids": [reply_to_event_id, *unseen_event_ids],
    }


def _build_cache_key(
    agent: Agent,
    full_prompt: str,
    session_id: str,
    *,
    show_tool_calls: bool | None = None,
    enrichment_digest: str | None = None,
) -> str:
    model = agent.model
    assert model is not None
    key = f"{agent.name}:{model.__class__.__name__}:{model.id}:{full_prompt}:{session_id}"
    if enrichment_digest is not None:
        key = f"{key}:enrichment={enrichment_digest}"
    if show_tool_calls is None:
        return key
    visibility = "show" if show_tool_calls else "hide"
    return f"{key}:tool_calls={visibility}"


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


async def _cached_agent_run(
    agent: Agent,
    full_prompt: str,
    session_id: str,
    agent_name: str,
    *,
    runtime_paths: RuntimePaths,
    user_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    media: MediaInputs | None = None,
    metadata: dict[str, Any] | None = None,
    prepared_history: PreparedHistoryState | None = None,
    enrichment_digest: str | None = None,
) -> RunOutput:
    """Cached wrapper for agent.arun() calls."""
    media_inputs = media or MediaInputs()
    storage_path = runtime_paths.storage_root
    history_state_requires_bypass = prepared_history is not None and prepared_history.has_persisted_history
    cache = (
        None
        if media_inputs.has_any() or history_state_requires_bypass
        else _get_cache(storage_path, runtime_ai_cache_enabled(runtime_paths=runtime_paths))
    )
    if cache is None:
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

    model = agent.model
    assert model is not None
    cache_key = _build_cache_key(
        agent,
        full_prompt,
        session_id,
        enrichment_digest=enrichment_digest,
    )
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        logger.info("Cache hit", agent=agent_name)
        return cast("RunOutput", cached_result)

    _note_attempt_run_id(run_id_callback, run_id)
    response = await agent.arun(
        full_prompt,
        session_id=session_id,
        user_id=user_id,
        run_id=run_id,
        metadata=metadata,
    )

    if response.status not in {RunStatus.cancelled, RunStatus.error}:
        cache.set(cache_key, response)
        logger.info("Response cached", agent=agent_name)

    return response


async def _prepare_agent_and_prompt(
    agent_name: str,
    prompt: str,
    runtime_paths: RuntimePaths,
    config: Config,
    thread_history: list[dict[str, Any]] | None = None,
    knowledge: Knowledge | None = None,
    include_interactive_questions: bool = True,
    session_id: str | None = None,
    reply_to_event_id: str | None = None,
    active_event_ids: Collection[str] = frozenset(),
    execution_identity: ToolExecutionIdentity | None = None,
    compaction_outcomes_collector: list[CompactionOutcome] | None = None,
    delegation_depth: int = 0,
) -> tuple[Agent, str, list[str], PreparedHistoryState]:
    """Prepare agent and full prompt for AI processing.

    Returns:
        Tuple of (agent, full_prompt, unseen_event_ids, prepared_history).
        unseen_event_ids is the list of event_ids injected as unseen context
        (empty when using the fallback path).

    """
    storage_path = runtime_paths.storage_root
    enhanced_prompt = await build_memory_enhanced_prompt(
        prompt,
        agent_name,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )

    storage = None
    session = None
    if session_id:
        storage = create_session_storage(
            agent_name,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        )
        session = get_agent_session(storage, session_id)

    agent = create_agent(
        agent_name,
        config,
        runtime_paths,
        knowledge=knowledge,
        include_interactive_questions=include_interactive_questions,
        execution_identity=execution_identity,
        delegation_depth=delegation_depth,
    )

    unseen_event_ids: list[str] = []
    prompt_with_unseen = enhanced_prompt
    if reply_to_event_id and thread_history:
        scope = resolve_history_scope(agent)
        seen_ids = read_scope_seen_event_ids(session, scope) if session is not None and scope is not None else set()
        matrix_id = config.get_ids(runtime_paths).get(agent_name)
        prompt_with_unseen, unseen_event_ids = build_prompt_with_unseen_thread_context(
            enhanced_prompt,
            thread_history,
            seen_event_ids=seen_ids,
            current_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=matrix_id.full_id if matrix_id else None,
        )

    fallback_prompt = (
        None
        if reply_to_event_id and thread_history
        else build_prompt_with_thread_history(
            enhanced_prompt,
            thread_history,
        )
    )
    prepared_history = await prepare_history_for_run(
        agent=agent,
        agent_name=agent_name,
        full_prompt=prompt_with_unseen,
        session_id=session_id,
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=execution_identity,
        compaction_outcomes_collector=compaction_outcomes_collector,
        storage=storage,
        session=session,
        static_prompt_tokens=estimate_preparation_static_tokens(
            agent,
            full_prompt=prompt_with_unseen,
            fallback_full_prompt=fallback_prompt,
        ),
    )
    if reply_to_event_id and thread_history:
        matrix_id = config.get_ids(runtime_paths).get(agent_name)
        full_prompt, unseen_event_ids = build_prompt_with_unseen_thread_context(
            enhanced_prompt,
            thread_history,
            seen_event_ids=seen_ids,
            current_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            response_sender_id=matrix_id.full_id if matrix_id else None,
        )
    elif prepared_history.has_persisted_history:
        full_prompt = enhanced_prompt
    else:
        full_prompt = build_prompt_with_thread_history(enhanced_prompt, thread_history)

    logger.info("Preparing agent and prompt", agent=agent_name, full_prompt=full_prompt)
    return agent, full_prompt, unseen_event_ids, prepared_history


async def ai_response(  # noqa: C901
    agent_name: str,
    prompt: str,
    session_id: str,
    runtime_paths: RuntimePaths,
    config: Config,
    thread_history: list[dict[str, Any]] | None = None,
    room_id: str | None = None,
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
    enrichment_digest: str | None = None,
    delegation_depth: int = 0,
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
        enrichment_digest: Optional digest of hook-provided enrichment used to vary the local cache key.
        delegation_depth: Current nested delegation depth for delegated-agent runs.

    Returns:
        Agent response string

    """
    logger.info("AI request", agent=agent_name, room_id=room_id)
    media_inputs = media or MediaInputs()
    agent: Agent | None = None
    prepared_history = PreparedHistoryState()

    try:
        agent, full_prompt, unseen_event_ids, prepared_history = await _prepare_agent_and_prompt(
            agent_name,
            prompt,
            runtime_paths,
            config,
            thread_history,
            knowledge,
            include_interactive_questions=include_interactive_questions,
            session_id=session_id,
            reply_to_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            execution_identity=execution_identity,
            compaction_outcomes_collector=compaction_outcomes_collector,
            delegation_depth=delegation_depth,
        )
    except Exception as e:
        logger.exception("Error preparing agent", agent=agent_name)
        return get_user_friendly_error_message(e, agent_name)

    try:
        metadata = build_matrix_run_metadata(reply_to_event_id, unseen_event_ids)

        response: RunOutput | None = None
        attempt_prompt = full_prompt
        attempt_media_inputs = media_inputs
        attempt_run_id = run_id

        for retried_without_inline_media in (False, True):
            try:
                response = await _cached_agent_run(
                    agent,
                    attempt_prompt,
                    session_id,
                    agent_name,
                    runtime_paths=runtime_paths,
                    user_id=user_id,
                    run_id=attempt_run_id,
                    run_id_callback=run_id_callback,
                    media=attempt_media_inputs,
                    metadata=metadata,
                    prepared_history=prepared_history,
                    enrichment_digest=enrichment_digest,
                )
            except Exception as e:
                if not retried_without_inline_media and should_retry_without_inline_media(e, attempt_media_inputs):
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

            break

        assert response is not None

        if tool_trace_collector is not None:
            tool_trace_collector.extend(_extract_tool_trace(response))
        if run_metadata_collector is not None:
            run_metadata = _build_ai_run_metadata_content(
                agent_name=agent_name,
                config=config,
                run_id=response.run_id,
                session_id=response.session_id or session_id,
                status=response.status,
                model=response.model,
                model_provider=response.model_provider,
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
        # Native Agno replay no longer binds transient per-run history state.
        pass


async def _process_stream_events(  # noqa: C901
    stream_generator: AsyncIterator[object],
    *,
    state: _StreamingAttemptState,
    show_tool_calls: bool,
    agent_name: str,
    media_inputs: MediaInputs,
    retried_without_inline_media: bool,
) -> AsyncGenerator[AIStreamChunk, None]:
    """Consume one streaming attempt, yielding chunks and mutating *state*."""
    try:
        async for event in stream_generator:
            if isinstance(event, RunContentEvent) and event.content:
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
    thread_history: list[dict[str, Any]] | None = None,
    room_id: str | None = None,
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
    enrichment_digest: str | None = None,
    delegation_depth: int = 0,
) -> AsyncIterator[AIStreamChunk]:
    """Generate streaming AI response using Agno's streaming API.

    Checks cache first - if found, yields the cached response immediately.
    Otherwise streams the new response and caches it.

    Args:
        agent_name: Name of the agent to use
        prompt: User prompt
        session_id: Session ID for conversation tracking
        runtime_paths: Runtime config/storage paths for agent data and config-aware tools
        config: Application configuration
        thread_history: Optional thread history
        room_id: Optional Matrix room ID for caller context
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
        enrichment_digest: Optional digest of hook-provided enrichment used to vary the local cache key.
        delegation_depth: Current nested delegation depth for delegated-agent runs.

    Yields:
        Streaming chunks/events as they become available

    """
    logger.info("AI streaming request", agent=agent_name, room_id=room_id)
    media_inputs = media or MediaInputs()
    storage_path = runtime_paths.storage_root
    agent: Agent | None = None
    prepared_history = PreparedHistoryState()

    try:
        agent, full_prompt, unseen_event_ids, prepared_history = await _prepare_agent_and_prompt(
            agent_name,
            prompt,
            runtime_paths,
            config,
            thread_history,
            knowledge,
            include_interactive_questions=include_interactive_questions,
            session_id=session_id,
            reply_to_event_id=reply_to_event_id,
            active_event_ids=active_event_ids,
            execution_identity=execution_identity,
            compaction_outcomes_collector=compaction_outcomes_collector,
            delegation_depth=delegation_depth,
        )
    except Exception as e:
        logger.exception("Error preparing agent for streaming", agent=agent_name)
        yield get_user_friendly_error_message(e, agent_name)
        return

    try:
        metadata = build_matrix_run_metadata(reply_to_event_id, unseen_event_ids)

        history_state_requires_bypass = prepared_history.has_persisted_history
        cache = (
            None
            if media_inputs.has_any() or history_state_requires_bypass
            else _get_cache(storage_path, runtime_ai_cache_enabled(runtime_paths=runtime_paths))
        )
        if cache is not None:
            model = agent.model
            assert model is not None
            cache_key = _build_cache_key(
                agent,
                full_prompt,
                session_id,
                show_tool_calls=show_tool_calls,
                enrichment_digest=enrichment_digest,
            )
            cached_result = cache.get(cache_key)
            if cached_result is not None:
                cached_run = cast("RunOutput", cached_result)
                logger.info("Cache hit", agent=agent_name)
                response_text = cached_run.content or ""
                if run_metadata_collector is not None:
                    cached_metadata = _build_ai_run_metadata_content(
                        agent_name=agent_name,
                        config=config,
                        run_id=cached_run.run_id,
                        session_id=cached_run.session_id or session_id,
                        status="cached",
                        model=cached_run.model,
                        model_provider=cached_run.model_provider,
                        metrics=cached_run.metrics,
                        tool_count=len(cached_run.tools) if cached_run.tools else 0,
                    )
                    if cached_metadata:
                        run_metadata_collector.update(cached_metadata)
                yield response_text
                return
        else:
            cache_key = None

        attempt_prompt = full_prompt
        attempt_media_inputs = media_inputs
        attempt_run_id = run_id
        state = _StreamingAttemptState()

        for retried_without_inline_media in (False, True):
            state = _StreamingAttemptState()

            try:
                _note_attempt_run_id(run_id_callback, attempt_run_id)
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

            async for stream_chunk in _process_stream_events(
                stream_generator,
                state=state,
                show_tool_calls=show_tool_calls,
                agent_name=agent_name,
                media_inputs=attempt_media_inputs,
                retried_without_inline_media=retried_without_inline_media,
            ):
                yield stream_chunk

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
                        run_id=state.cancelled_run_event.run_id,
                        session_id=state.cancelled_run_event.session_id or session_id,
                        status=RunStatus.cancelled,
                        model=state.latest_model_id,
                        model_provider=state.latest_model_provider,
                        metrics=fallback_metrics,
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
                run_id=state.completed_run_event.run_id if state.completed_run_event is not None else None,
                session_id=(
                    state.completed_run_event.session_id
                    if state.completed_run_event is not None and state.completed_run_event.session_id is not None
                    else session_id
                ),
                status=RunStatus.completed,
                model=state.latest_model_id,
                model_provider=state.latest_model_provider,
                metrics=state.completed_run_event.metrics if state.completed_run_event is not None else None,
                metrics_fallback=fallback_metrics,
                tool_count=(
                    len(state.completed_run_event.tools)
                    if state.completed_run_event is not None and state.completed_run_event.tools is not None
                    else state.observed_tool_calls
                ),
            )
            if run_metadata:
                run_metadata_collector.update(run_metadata)

        if cache is not None and cache_key is not None and state.full_response:
            cached_response = RunOutput(content=state.full_response)
            cache.set(cache_key, cached_response)
            logger.info("Response cached", agent=agent_name)
    finally:
        # Native Agno replay no longer binds transient per-run history state.
        pass
