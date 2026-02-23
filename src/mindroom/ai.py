"""AI integration module for MindRoom agents and memory management."""

from __future__ import annotations

import functools
import os
from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

import diskcache
from agno.models.anthropic import Claude
from agno.models.cerebras import Cerebras
from agno.models.deepseek import DeepSeek
from agno.models.google import Gemini
from agno.models.groq import Groq
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.models.openrouter import OpenRouter
from agno.run.agent import RunContentEvent, RunErrorEvent, RunOutput, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.base import RunStatus
from agno.utils.message import filter_tool_calls

from .agents import _get_agent_session, create_agent, create_session_storage, get_seen_event_ids
from .constants import ENABLE_AI_CACHE, PROVIDER_ENV_KEYS
from .credentials import get_credentials_manager
from .credentials_sync import get_api_key_for_provider, get_ollama_host
from .error_handling import get_user_friendly_error_message
from .logging_config import get_logger
from .memory import build_memory_enhanced_prompt
from .tool_events import (
    complete_pending_tool_block,
    extract_tool_completed_info,
    format_tool_combined,
    format_tool_started_event,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from agno.agent import Agent
    from agno.knowledge.knowledge import Knowledge
    from agno.media import Image
    from agno.models.base import Model
    from agno.session.agent import AgentSession

    from .config import Config, ModelConfig
    from .tool_events import ToolTraceEntry

logger = get_logger(__name__)

AIStreamChunk = str | RunContentEvent | ToolCallStartedEvent | ToolCallCompletedEvent


def _estimate_message_media_chars(message: object) -> int:
    media_fields = (
        "images",
        "audio",
        "videos",
        "files",
        "audio_output",
        "image_output",
        "video_output",
        "file_output",
    )
    media_chars = 0
    for field in media_fields:
        media_value = getattr(message, field, None)
        if media_value:
            media_chars += len(str(media_value))
    return media_chars


def _estimate_messages_tokens(messages: Sequence[Any] | None) -> int:
    """Estimate token count for messages using chars / 4 approximation."""
    if not messages:
        return 0
    total_chars = 0
    for msg in messages:
        content = getattr(msg, "compressed_content", None) or getattr(msg, "content", None)
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                total_chars += len(str(part))
        elif content is not None:
            total_chars += len(str(content))
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            total_chars += len(str(tool_calls))
        total_chars += _estimate_message_media_chars(msg)
    return total_chars // 4


def _estimate_static_tokens(agent: Agent, full_prompt: str) -> int:
    """Estimate tokens for the system prompt and current user message (chars / 4)."""
    static_chars = len(agent.role or "")
    instructions = agent.instructions
    if isinstance(instructions, str):
        static_chars += len(instructions)
    elif isinstance(instructions, list):
        for instruction in instructions:
            static_chars += len(str(instruction))
    static_chars += len(full_prompt)
    return static_chars // 4


def _get_history_skip_roles(agent: Agent) -> list[str] | None:
    """Return history skip_roles matching Agno's run-message construction."""
    system_role = getattr(agent, "system_message_role", None)
    if isinstance(system_role, str) and system_role not in {"user", "assistant", "tool"}:
        return [system_role]
    return None


def _get_team_scope(agent: Agent) -> tuple[str | None, str | None]:
    """Return (team_id, agent_id) only when agent is actually in a team scope."""
    team_id = getattr(agent, "team_id", None)
    if not isinstance(team_id, str) or not team_id:
        return None, None
    agent_id = getattr(agent, "id", None)
    return team_id, agent_id if isinstance(agent_id, str) and agent_id else None


def _get_replayable_runs(session: AgentSession, agent: Agent) -> list[RunOutput]:
    """Get runs eligible for history replay, matching Agno session filtering."""
    runs = [run for run in session.runs or [] if isinstance(run, RunOutput)]

    team_id, agent_id = _get_team_scope(agent)
    if team_id is not None:
        if agent_id:
            runs = [run for run in runs if run.agent_id == agent_id]
        runs = [run for run in runs if getattr(run, "team_id", None) == team_id]

    skip_statuses = {RunStatus.paused, RunStatus.cancelled, RunStatus.error}
    return [run for run in runs if run.parent_run_id is None and run.status not in skip_statuses]


def _estimate_history_tokens(session: AgentSession, agent: Agent, run_limit: int | None) -> int:
    """Estimate tokens for replayed history messages using Agno's get_messages path."""
    team_id, agent_id = _get_team_scope(agent)
    messages = session.get_messages(
        agent_id=agent_id,
        team_id=team_id,
        last_n_runs=run_limit,
        limit=None,
        skip_roles=_get_history_skip_roles(agent),
    )
    max_tool_calls_from_history = getattr(agent, "max_tool_calls_from_history", None)
    if max_tool_calls_from_history is None:
        return _estimate_messages_tokens(messages)
    history_copy = [deepcopy(msg) for msg in messages]
    filter_tool_calls(history_copy, max_tool_calls_from_history)
    return _estimate_messages_tokens(history_copy)


def _find_fitting_run_limit(session: AgentSession, agent: Agent, max_runs: int, budget: int) -> int:
    """Find the largest run limit whose replayed history stays within budget."""
    low = 0
    high = max_runs
    best = 0

    while low <= high:
        mid = (low + high) // 2
        tokens = 0 if mid == 0 else _estimate_history_tokens(session, agent, mid)
        if tokens <= budget:
            best = mid
            low = mid + 1
        else:
            high = mid - 1

    return best


def _disable_history_for_run(
    agent: Agent,
    *,
    reason: str,
    agent_name: str,
    context_window: int,
    threshold: int,
) -> None:
    """Disable history context for this run when no safe history budget remains."""
    if not agent.add_history_to_context:
        return
    agent.add_history_to_context = False
    logger.warning(
        "Context window limit approaching, disabling history for this run",
        agent=agent_name,
        reason=reason,
        context_window=context_window,
        threshold=threshold,
    )


def _apply_context_window_limit(
    agent: Agent,
    agent_name: str,
    config: Config,
    full_prompt: str,
    session_id: str | None,
    storage_path: Path,
    session: AgentSession | None = None,
) -> None:
    """Dynamically reduce ``agent.num_history_runs`` when the estimated context approaches the model's context window.

    Uses chars/4 token estimation and an 80 % threshold to leave headroom
    for the model response and tool definitions.  Only applies to run-based
    history limits (skipped when ``num_history_messages`` is set).

    Pass a pre-loaded *session* to avoid a redundant SQLite read when the
    caller already loaded it.
    """
    if agent.num_history_messages is not None or not session_id:
        return

    model_name = config.get_entity_model_name(agent_name)
    model_config = config.models.get(model_name)
    if model_config is None or model_config.context_window is None:
        return

    context_window = model_config.context_window
    threshold = int(context_window * 0.8)
    static_tokens = _estimate_static_tokens(agent, full_prompt)

    # Use pre-loaded session or load from storage
    if session is None:
        storage = create_session_storage(agent_name, storage_path)
        session = _get_agent_session(storage, session_id)
    if not session or not session.runs:
        return

    replayable_runs = _get_replayable_runs(session, agent)
    if not replayable_runs:
        return

    # Determine how many runs the agent currently considers
    current_limit = agent.num_history_runs
    max_considered_runs = (
        min(current_limit, len(replayable_runs))
        if current_limit is not None and current_limit > 0
        else len(replayable_runs)
    )
    initial_run_limit = max_considered_runs if current_limit is not None and current_limit > 0 else None
    history_tokens = _estimate_history_tokens(session, agent, initial_run_limit)
    total_tokens = static_tokens + history_tokens
    if total_tokens <= threshold:
        return

    original = current_limit if current_limit is not None else len(replayable_runs)
    budget = threshold - static_tokens
    if budget <= 0:
        new_limit = 0
        reason = "no_history_budget"
    else:
        new_limit = _find_fitting_run_limit(session, agent, max_considered_runs, budget)
        reason = "history_exceeds_budget"

    if new_limit == 0:
        _disable_history_for_run(
            agent,
            reason=reason,
            agent_name=agent_name,
            context_window=context_window,
            threshold=threshold,
        )
        return

    if new_limit < original:
        agent.num_history_runs = new_limit
        logger.warning(
            "Context window limit approaching, reducing history",
            agent=agent_name,
            original_runs=original,
            reduced_runs=new_limit,
            estimated_tokens=total_tokens,
            context_window=context_window,
            threshold=threshold,
        )


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


@functools.cache
def get_cache(storage_path: Path) -> diskcache.Cache | None:
    """Get or create a cache instance for the given storage path."""
    return diskcache.Cache(storage_path / ".ai_cache") if ENABLE_AI_CACHE else None


def _set_api_key_env_var(provider: str) -> None:
    """Set environment variable for a provider from CredentialsManager.

    Since we sync from .env to CredentialsManager on startup,
    this will always use the latest keys from .env.

    Args:
        provider: Provider name (e.g., 'openai', 'anthropic')

    """
    env_vars = {**PROVIDER_ENV_KEYS, "gemini": PROVIDER_ENV_KEYS["google"]}

    if provider not in env_vars:
        return

    # Get API key from CredentialsManager (which has been synced from .env)
    api_key = get_api_key_for_provider(provider)

    # Set environment variable if key exists
    if api_key:
        os.environ[env_vars[provider]] = api_key
        logger.debug(f"Set {env_vars[provider]} from CredentialsManager")


def _create_model_for_provider(provider: str, model_id: str, model_config: ModelConfig, extra_kwargs: dict) -> Model:
    """Create a model instance for a specific provider.

    Args:
        provider: The AI provider name
        model_id: The model identifier
        model_config: The model configuration object
        extra_kwargs: Additional keyword arguments for the model

    Returns:
        Instantiated model for the provider

    Raises:
        ValueError: If provider not supported

    """
    # Handle Ollama separately due to special host configuration
    if provider == "ollama":
        # Priority: model config > env/CredentialsManager > default
        # This allows per-model host configuration in config.yaml
        host = model_config.host or get_ollama_host() or "http://localhost:11434"
        logger.debug(f"Using Ollama host: {host}")
        return Ollama(id=model_id, host=host, **extra_kwargs)

    # Handle OpenRouter separately due to API key capture timing issue
    if provider == "openrouter":
        # OpenRouter needs the API key passed explicitly because it captures
        # the environment variable at import time, not at instantiation time
        api_key = extra_kwargs.pop("api_key", None) or get_api_key_for_provider(provider)
        if not api_key:
            logger.warning("No OpenRouter API key found in environment or CredentialsManager")
        return OpenRouter(id=model_id, api_key=api_key, **extra_kwargs)

    # Map providers to their model classes for simple instantiation
    provider_map: dict[str, type[Model]] = {
        "openai": OpenAIChat,
        "anthropic": Claude,
        "gemini": Gemini,
        "google": Gemini,
        "cerebras": Cerebras,
        "groq": Groq,
        "deepseek": DeepSeek,
    }

    model_class = provider_map.get(provider)
    if model_class is not None:
        return model_class(id=model_id, **extra_kwargs)

    msg = f"Unsupported AI provider: {provider}"
    raise ValueError(msg)


def get_model_instance(config: Config, model_name: str = "default") -> Model:
    """Get a model instance from config.yaml.

    Args:
        config: Application configuration
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
    creds_manager = get_credentials_manager()
    model_creds = creds_manager.load_credentials(f"model:{model_name}")
    model_api_key = model_creds.get("api_key") if model_creds else None

    if model_api_key:
        extra_kwargs["api_key"] = model_api_key
    else:
        # Set environment variable from CredentialsManager for Agno to use
        _set_api_key_env_var(provider)

    return _create_model_for_provider(provider, model_id, model_config, extra_kwargs)


def _format_messages_context(messages: list[dict[str, Any]], header: str, prompt: str) -> str:
    """Format messages as context prepended to a prompt."""
    context_lines: list[str] = []
    for msg in messages:
        sender = msg.get("sender")
        body = msg.get("body")
        if sender and body:
            context_lines.append(f"{sender}: {body}")
    if not context_lines:
        return prompt
    context = "\n".join(context_lines)
    return f"{header}\n{context}\n\nCurrent message:\n{prompt}"


def build_prompt_with_thread_history(prompt: str, thread_history: list[dict[str, Any]] | None = None) -> str:
    """Build a prompt with thread history context when available."""
    if not thread_history:
        return prompt
    return _format_messages_context(thread_history, "Previous conversation in this thread:", prompt)


def _get_unseen_messages(
    thread_history: list[dict[str, Any]],
    agent_name: str,
    config: Config,
    seen_event_ids: set[str],
    current_event_id: str | None,
) -> list[dict[str, Any]]:
    """Filter thread_history to messages not yet consumed by this agent.

    Excludes:
    - Messages from this agent (by Matrix user ID)
    - Messages whose event_id is in seen_event_ids
    - The current triggering message (current_event_id)
    """
    matrix_id = config.ids.get(agent_name)
    agent_sender_id = matrix_id.full_id if matrix_id else None
    unseen: list[dict[str, Any]] = []
    for msg in thread_history:
        event_id = msg.get("event_id")
        sender = msg.get("sender")
        # Skip messages from this agent
        if agent_sender_id and sender == agent_sender_id:
            continue
        # Skip already-seen messages
        if event_id and event_id in seen_event_ids:
            continue
        # Skip the current triggering message
        if current_event_id and event_id == current_event_id:
            continue
        unseen.append(msg)
    return unseen


def _build_prompt_with_unseen(prompt: str, unseen_messages: list[dict[str, Any]]) -> str:
    """Prepend unseen messages from other participants to the prompt."""
    if not unseen_messages:
        return prompt
    return _format_messages_context(
        unseen_messages,
        "Messages from other participants since your last response:",
        prompt,
    )


def _build_run_metadata(reply_to_event_id: str | None, unseen_event_ids: list[str]) -> dict[str, Any] | None:
    """Build metadata dict for a run, tracking consumed Matrix event_ids."""
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
) -> str:
    model = agent.model
    assert model is not None
    key = f"{agent.name}:{model.__class__.__name__}:{model.id}:{full_prompt}:{session_id}"
    if show_tool_calls is None:
        return key
    visibility = "show" if show_tool_calls else "hide"
    return f"{key}:tool_calls={visibility}"


async def _cached_agent_run(
    agent: Agent,
    full_prompt: str,
    session_id: str,
    agent_name: str,
    storage_path: Path,
    user_id: str | None = None,
    images: Sequence[Image] | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunOutput:
    """Cached wrapper for agent.arun() calls."""
    # Skip cache when images are present (large bytes, unlikely to repeat)
    # or when Agno history is enabled (prompt can be identical but replayed history differs)
    cache = None if (images or agent.add_history_to_context) else get_cache(storage_path)
    if cache is None:
        return await agent.arun(
            full_prompt,
            session_id=session_id,
            user_id=user_id,
            images=images,
            metadata=metadata,
        )

    model = agent.model
    assert model is not None
    cache_key = _build_cache_key(agent, full_prompt, session_id)
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        logger.info("Cache hit", agent=agent_name)
        return cast("RunOutput", cached_result)

    response = await agent.arun(full_prompt, session_id=session_id, user_id=user_id, metadata=metadata)

    cache.set(cache_key, response)
    logger.info("Response cached", agent=agent_name)

    return response


async def _prepare_agent_and_prompt(
    agent_name: str,
    prompt: str,
    storage_path: Path,
    room_id: str | None,
    config: Config,
    thread_history: list[dict[str, Any]] | None = None,
    knowledge: Knowledge | None = None,
    include_interactive_questions: bool = True,
    session_id: str | None = None,
    reply_to_event_id: str | None = None,
) -> tuple[Agent, str, list[str]]:
    """Prepare agent and full prompt for AI processing.

    Returns:
        Tuple of (agent, full_prompt, unseen_event_ids).
        unseen_event_ids is the list of event_ids injected as unseen context
        (empty when using the fallback path).

    """
    enhanced_prompt = await build_memory_enhanced_prompt(prompt, agent_name, storage_path, config, room_id)

    unseen_event_ids: list[str] = []

    # Check whether Agno already has prior runs for this session.
    session = None
    has_prior_runs = False
    if session_id and thread_history:
        storage = create_session_storage(agent_name, storage_path)
        session = _get_agent_session(storage, session_id)
        has_prior_runs = session is not None and bool(session.runs)

    if has_prior_runs and reply_to_event_id:
        # Matrix bot path: Agno replays history natively, inject only unseen messages.
        assert session is not None
        assert thread_history is not None
        seen_ids = get_seen_event_ids(session)
        unseen = _get_unseen_messages(thread_history, agent_name, config, seen_ids, reply_to_event_id)
        unseen_event_ids = [msg["event_id"] for msg in unseen if msg.get("event_id")]
        full_prompt = _build_prompt_with_unseen(enhanced_prompt, unseen)
    elif has_prior_runs and not reply_to_event_id:
        # Non-Matrix path (OpenAI-compat): Agno replays history natively.
        # No unseen detection (thread_history entries lack event_id fields).
        full_prompt = enhanced_prompt
    else:
        # No prior runs (first turn / storage lost / no session_id) → fallback.
        full_prompt = build_prompt_with_thread_history(enhanced_prompt, thread_history)

    logger.info("Preparing agent and prompt", agent=agent_name, full_prompt=full_prompt)
    agent = create_agent(
        agent_name,
        config,
        storage_path=storage_path,
        knowledge=knowledge,
        include_interactive_questions=include_interactive_questions,
    )
    _apply_context_window_limit(agent, agent_name, config, full_prompt, session_id, storage_path, session=session)
    return agent, full_prompt, unseen_event_ids


async def ai_response(
    agent_name: str,
    prompt: str,
    session_id: str,
    storage_path: Path,
    config: Config,
    thread_history: list[dict[str, Any]] | None = None,
    room_id: str | None = None,
    knowledge: Knowledge | None = None,
    user_id: str | None = None,
    include_interactive_questions: bool = True,
    images: Sequence[Image] | None = None,
    reply_to_event_id: str | None = None,
    show_tool_calls: bool = True,
    tool_trace_collector: list[ToolTraceEntry] | None = None,
) -> str:
    """Generates a response using the specified agno Agent with memory integration.

    Args:
        agent_name: Name of the agent to use
        prompt: User prompt
        session_id: Session ID for conversation tracking
        storage_path: Path for storing agent data
        config: Application configuration
        thread_history: Optional thread history
        room_id: Optional room ID for room memory access
        knowledge: Optional shared knowledge base for RAG-enabled agents
        user_id: Matrix user ID of the sender, used by Agno's LearningMachine
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        images: Optional images to pass to the AI model for vision analysis
        reply_to_event_id: Matrix event ID of the triggering message, stored
            in run metadata for unseen message tracking and edit cleanup.
        show_tool_calls: Whether to include tool call details inline in the response text.
        tool_trace_collector: Optional list that receives structured tool-trace
            entries from this run.

    Returns:
        Agent response string

    """
    logger.info("AI request", agent=agent_name)

    # Prepare agent and prompt - this can fail if agent creation fails (e.g., missing API key)
    try:
        agent, full_prompt, unseen_event_ids = await _prepare_agent_and_prompt(
            agent_name,
            prompt,
            storage_path,
            room_id,
            config,
            thread_history,
            knowledge,
            include_interactive_questions=include_interactive_questions,
            session_id=session_id,
            reply_to_event_id=reply_to_event_id,
        )
    except Exception as e:
        logger.exception("Error preparing agent", agent=agent_name)
        return get_user_friendly_error_message(e, agent_name)

    metadata = _build_run_metadata(reply_to_event_id, unseen_event_ids)

    # Execute the AI call - this can fail for network, rate limits, etc.
    try:
        response = await _cached_agent_run(
            agent,
            full_prompt,
            session_id,
            agent_name,
            storage_path,
            user_id=user_id,
            images=images,
            metadata=metadata,
        )
    except Exception as e:
        logger.exception("Error generating AI response", agent=agent_name)
        return get_user_friendly_error_message(e, agent_name)

    if tool_trace_collector is not None:
        tool_trace_collector.extend(_extract_tool_trace(response))

    # Extract response content - this shouldn't fail
    return _extract_response_content(response, show_tool_calls=show_tool_calls)


async def stream_agent_response(  # noqa: C901, PLR0912, PLR0915
    agent_name: str,
    prompt: str,
    session_id: str,
    storage_path: Path,
    config: Config,
    thread_history: list[dict[str, Any]] | None = None,
    room_id: str | None = None,
    knowledge: Knowledge | None = None,
    user_id: str | None = None,
    include_interactive_questions: bool = True,
    images: Sequence[Image] | None = None,
    reply_to_event_id: str | None = None,
    show_tool_calls: bool = True,
) -> AsyncIterator[AIStreamChunk]:
    """Generate streaming AI response using Agno's streaming API.

    Checks cache first - if found, yields the cached response immediately.
    Otherwise streams the new response and caches it.

    Args:
        agent_name: Name of the agent to use
        prompt: User prompt
        session_id: Session ID for conversation tracking
        storage_path: Path for storing agent data
        config: Application configuration
        thread_history: Optional thread history
        room_id: Optional room ID for room memory access
        knowledge: Optional shared knowledge base for RAG-enabled agents
        user_id: Matrix user ID of the sender, used by Agno's LearningMachine
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        images: Optional images to pass to the AI model for vision analysis
        reply_to_event_id: Matrix event ID of the triggering message, stored
            in run metadata for unseen message tracking and edit cleanup.
        show_tool_calls: Whether to include tool call details inline in the streamed response.

    Yields:
        Streaming chunks/events as they become available

    """
    logger.info("AI streaming request", agent=agent_name)

    # Prepare agent and prompt - this can fail if agent creation fails
    try:
        agent, full_prompt, unseen_event_ids = await _prepare_agent_and_prompt(
            agent_name,
            prompt,
            storage_path,
            room_id,
            config,
            thread_history,
            knowledge,
            include_interactive_questions=include_interactive_questions,
            session_id=session_id,
            reply_to_event_id=reply_to_event_id,
        )
    except Exception as e:
        logger.exception("Error preparing agent for streaming", agent=agent_name)
        yield get_user_friendly_error_message(e, agent_name)
        return

    metadata = _build_run_metadata(reply_to_event_id, unseen_event_ids)

    # Check cache (skip when images are present or history is enabled)
    cache = None if (images or agent.add_history_to_context) else get_cache(storage_path)
    if cache is not None:
        model = agent.model
        assert model is not None
        cache_key = _build_cache_key(agent, full_prompt, session_id, show_tool_calls=show_tool_calls)
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            logger.info("Cache hit", agent=agent_name)
            response_text = cached_result.content or ""
            yield response_text
            return

    full_response = ""
    tool_count = 0
    pending_tools: list[tuple[str, int]] = []

    # Execute the streaming AI call - this can fail for network, rate limits, etc.
    try:
        stream_generator = agent.arun(
            full_prompt,
            session_id=session_id,
            user_id=user_id,
            images=images,
            stream=True,
            stream_events=True,
            metadata=metadata,
        )
    except Exception as e:
        logger.exception("Error starting streaming AI response")
        yield get_user_friendly_error_message(e, agent_name)
        return

    # Process the stream events
    try:
        async for event in stream_generator:
            if isinstance(event, RunContentEvent) and event.content:
                chunk_text = str(event.content)
                full_response += chunk_text
                yield event
            elif isinstance(event, ToolCallStartedEvent):
                if show_tool_calls:
                    tool_count += 1
                    tool_msg, trace_entry = format_tool_started_event(event.tool, tool_index=tool_count)
                    if tool_msg:
                        full_response += tool_msg
                    if trace_entry is not None:
                        pending_tools.append((trace_entry.tool_name, tool_count))
                yield event
            elif isinstance(event, ToolCallCompletedEvent):
                if show_tool_calls:
                    info = extract_tool_completed_info(event.tool)
                    if info:
                        tool_name, result = info
                        match_pos = next(
                            (
                                pos
                                for pos in range(len(pending_tools) - 1, -1, -1)
                                if pending_tools[pos][0] == tool_name
                            ),
                            None,
                        )
                        if match_pos is None:
                            logger.warning(
                                "Missing pending tool start in AI stream; skipping completion marker",
                                tool_name=tool_name,
                                agent=agent_name,
                            )
                            yield event
                            continue
                        _, tool_index = pending_tools.pop(match_pos)
                        full_response, _ = complete_pending_tool_block(
                            full_response,
                            tool_name,
                            result,
                            tool_index=tool_index,
                        )
                yield event
            elif isinstance(event, RunErrorEvent):
                error_text = event.content or "Unknown agent error"
                logger.error("Agent run error during streaming", agent=agent_name, error=error_text)
                yield get_user_friendly_error_message(Exception(error_text), agent_name)
                return
            else:
                logger.debug("Skipping stream event", event_type=type(event).__name__)
    except Exception as e:
        logger.exception("Error during streaming AI response")
        yield get_user_friendly_error_message(e, agent_name)
        return

    if cache is not None and full_response:
        cached_response = RunOutput(content=full_response)
        cache.set(cache_key, cached_response)
        logger.info("Response cached", agent=agent_name)
