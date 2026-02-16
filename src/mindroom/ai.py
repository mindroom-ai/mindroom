"""AI integration module for MindRoom agents and memory management."""

from __future__ import annotations

import functools
import os
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

from .agents import create_agent
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
from .workspace import load_workspace_memory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.agent import Agent
    from agno.knowledge.knowledge import Knowledge
    from agno.models.base import Model

    from .config import Config, ModelConfig

logger = get_logger(__name__)

AIStreamChunk = str | RunContentEvent | ToolCallStartedEvent | ToolCallCompletedEvent


def _extract_response_content(response: RunOutput) -> str:
    response_parts = []

    # Add main content if present
    if response.content:
        response_parts.append(response.content)

    # Add formatted tool call sections when present.
    if response.tools:
        tool_sections: list[str] = []
        for tool in response.tools:
            tool_name = tool.tool_name or "tool"
            tool_args = tool.tool_args or {}
            combined, _ = format_tool_combined(tool_name, tool_args, tool.result)
            tool_sections.append(combined.strip())
        if tool_sections:
            response_parts.append("\n\n".join(tool_sections))

    return "\n".join(response_parts) if response_parts else ""


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


def build_prompt_with_thread_history(prompt: str, thread_history: list[dict[str, Any]] | None = None) -> str:
    """Build a prompt with thread history context when available."""
    if not thread_history:
        return prompt

    context_lines: list[str] = []
    for message in thread_history:
        sender = message.get("sender")
        body = message.get("body")
        if sender and body:
            context_lines.append(f"{sender}: {body}")

    if not context_lines:
        return prompt

    context = "\n".join(context_lines)
    return f"Previous conversation in this thread:\n{context}\n\nCurrent message:\n{prompt}"


def _build_cache_key(agent: Agent, full_prompt: str, session_id: str) -> str:
    model = agent.model
    assert model is not None
    return f"{agent.name}:{model.__class__.__name__}:{model.id}:{full_prompt}:{session_id}"


async def _cached_agent_run(
    agent: Agent,
    full_prompt: str,
    session_id: str,
    agent_name: str,
    storage_path: Path,
    user_id: str | None = None,
) -> RunOutput:
    """Cached wrapper for agent.arun() calls."""
    cache = get_cache(storage_path)
    if cache is None:
        return await agent.arun(full_prompt, session_id=session_id, user_id=user_id)

    model = agent.model
    assert model is not None
    cache_key = _build_cache_key(agent, full_prompt, session_id)
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        logger.info("Cache hit", agent=agent_name)
        return cast("RunOutput", cached_result)

    response = await agent.arun(full_prompt, session_id=session_id, user_id=user_id)

    cache.set(cache_key, response)
    logger.info("Response cached", agent=agent_name)

    return response


async def _prepare_agent_and_prompt(
    agent_name: str,
    prompt: str,
    storage_path: Path,
    room_id: str | None,
    is_dm: bool,
    config: Config,
    thread_history: list[dict[str, Any]] | None = None,
    knowledge: Knowledge | None = None,
    include_default_tools: bool = True,
    include_interactive_questions: bool = True,
) -> tuple[Agent, str]:
    """Prepare agent and full prompt for AI processing.

    Returns:
        Tuple of (agent, full_prompt, session_id)

    """
    workspace_context = load_workspace_memory(
        agent_name,
        storage_path,
        config,
        room_id=room_id,
        is_dm=is_dm,
    )
    workspace_prompt = f"{workspace_context}\n\n{prompt}" if workspace_context else prompt
    enhanced_prompt = await build_memory_enhanced_prompt(workspace_prompt, agent_name, storage_path, config, room_id)
    full_prompt = build_prompt_with_thread_history(enhanced_prompt, thread_history)
    logger.info("Preparing agent and prompt", agent=agent_name, full_prompt=full_prompt)
    agent = create_agent(
        agent_name,
        config,
        storage_path=storage_path,
        knowledge=knowledge,
        include_default_tools=include_default_tools,
        include_interactive_questions=include_interactive_questions,
    )
    return agent, full_prompt


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
    include_default_tools: bool = True,
    include_interactive_questions: bool = True,
    is_dm: bool = False,
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
        include_default_tools: Whether to include default tools (e.g. scheduler).
            Set to False when calling outside of Matrix context.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        is_dm: Whether the current room is a DM/private room.

    Returns:
        Agent response string

    """
    logger.info("AI request", agent=agent_name)

    # Prepare agent and prompt - this can fail if agent creation fails (e.g., missing API key)
    try:
        agent, full_prompt = await _prepare_agent_and_prompt(
            agent_name,
            prompt,
            storage_path,
            room_id,
            is_dm,
            config,
            thread_history,
            knowledge,
            include_default_tools=include_default_tools,
            include_interactive_questions=include_interactive_questions,
        )
    except Exception as e:
        logger.exception("Error preparing agent", agent=agent_name)
        return get_user_friendly_error_message(e, agent_name)

    # Execute the AI call - this can fail for network, rate limits, etc.
    try:
        response = await _cached_agent_run(agent, full_prompt, session_id, agent_name, storage_path, user_id=user_id)
    except Exception as e:
        logger.exception("Error generating AI response", agent=agent_name)
        return get_user_friendly_error_message(e, agent_name)

    # Extract response content - this shouldn't fail
    return _extract_response_content(response)


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
    include_default_tools: bool = True,
    include_interactive_questions: bool = True,
    is_dm: bool = False,
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
        include_default_tools: Whether to include default tools (e.g. scheduler).
            Set to False when calling outside of Matrix context.
        include_interactive_questions: Whether to include the interactive
            question authoring prompt. Set to False for channels that do not
            support Matrix reaction-based question flows.
        is_dm: Whether the current room is a DM/private room.

    Yields:
        Streaming chunks/events as they become available

    """
    logger.info("AI streaming request", agent=agent_name)

    # Prepare agent and prompt - this can fail if agent creation fails
    try:
        agent, full_prompt = await _prepare_agent_and_prompt(
            agent_name,
            prompt,
            storage_path,
            room_id,
            is_dm,
            config,
            thread_history,
            knowledge,
            include_default_tools=include_default_tools,
            include_interactive_questions=include_interactive_questions,
        )
    except Exception as e:
        logger.exception("Error preparing agent for streaming", agent=agent_name)
        yield get_user_friendly_error_message(e, agent_name)
        return

    # Check cache (this shouldn't fail)
    cache = get_cache(storage_path)
    if cache is not None:
        model = agent.model
        assert model is not None
        cache_key = _build_cache_key(agent, full_prompt, session_id)
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            logger.info("Cache hit", agent=agent_name)
            response_text = cached_result.content or ""
            yield response_text
            return

    full_response = ""

    # Execute the streaming AI call - this can fail for network, rate limits, etc.
    try:
        stream_generator = agent.arun(
            full_prompt,
            session_id=session_id,
            user_id=user_id,
            stream=True,
            stream_events=True,
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
                tool_msg, _ = format_tool_started_event(event.tool)
                if tool_msg:
                    full_response += tool_msg
                    yield event
            elif isinstance(event, ToolCallCompletedEvent):
                info = extract_tool_completed_info(event.tool)
                if info:
                    tool_name, result = info
                    full_response, _ = complete_pending_tool_block(full_response, tool_name, result)
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
