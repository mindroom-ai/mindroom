"""AI integration module for MindRoom agents and memory management."""

from __future__ import annotations

import functools
import os
from typing import TYPE_CHECKING, Any

import diskcache
from agno.models.anthropic import Claude
from agno.models.cerebras import Cerebras
from agno.models.google import Gemini
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.models.openrouter import OpenRouter
from agno.run.response import (
    RunResponse,
    RunResponseContentEvent,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)

from .agents import create_agent
from .constants import ENABLE_AI_CACHE
from .credentials_sync import get_api_key_for_provider, get_ollama_host
from .error_handling import get_user_friendly_error_message
from .logging_config import get_logger
from .memory import build_memory_enhanced_prompt

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from agno.agent import Agent
    from agno.models.base import Model

    from .config import Config, ModelConfig

logger = get_logger(__name__)


def _extract_response_content(response: RunResponse) -> str:
    response_parts = []

    # Add main content if present
    if response.content:
        response_parts.append(response.content)

    # Add formatted tool calls if present (similar to agno's print_response)
    # Only add if there are actual tool calls to display
    if response.formatted_tool_calls and any(response.formatted_tool_calls):
        tool_calls_section = "\n\n**Tool Calls:**"
        for tool_call in response.formatted_tool_calls:
            tool_calls_section += f"\n• {tool_call}"
        response_parts.append(tool_calls_section)

    return "\n".join(response_parts) if response_parts else ""


def _format_tool_started_message(event: ToolCallStartedEvent) -> str:
    if not event.tool:
        return ""

    tool_name = event.tool.tool_name if event.tool.tool_name else "tool"
    tool_args = event.tool.tool_args if event.tool.tool_args else {}

    # Format similar to agno's formatted_tool_calls
    if tool_args:
        args_str = ", ".join(f"{k}={v}" for k, v in tool_args.items())
        msg = f"\n\n🔧 **Tool Call:** `{tool_name}({args_str})`\n"
    else:
        msg = f"\n\n🔧 **Tool Call:** `{tool_name}()`\n"

    return msg


def _format_tool_completed_message(event: ToolCallCompletedEvent) -> str:
    if not event.tool:
        return ""

    tool_name = event.tool.tool_name if event.tool.tool_name else "tool"

    # Check both event.content and tool.result for the output
    result = event.content or (event.tool.result if event.tool else None)

    if result:
        # Format the result nicely
        return f"✅ **`{tool_name}` result:**\n{result}\n\n"

    return f"✅ **`{tool_name}`** completed\n\n"


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
    # Map provider names to environment variable names
    env_vars = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "gemini": "GOOGLE_API_KEY",
        "google": "GOOGLE_API_KEY",
        "cerebras": "CEREBRAS_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "groq": "GROQ_API_KEY",
    }

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
    if provider == "ollama":
        # Priority: model config > env/CredentialsManager > default
        # This allows per-model host configuration in config.yaml
        host = model_config.host or get_ollama_host() or "http://localhost:11434"
        logger.debug(f"Using Ollama host: {host}")
        return Ollama(id=model_id, host=host, **extra_kwargs)
    if provider == "openai":
        return OpenAIChat(id=model_id, **extra_kwargs)
    if provider == "anthropic":
        return Claude(id=model_id, **extra_kwargs)
    if provider == "openrouter":
        # OpenRouter needs the API key passed explicitly because it captures
        # the environment variable at import time, not at instantiation time
        api_key = get_api_key_for_provider(provider)
        if not api_key:
            logger.warning("No OpenRouter API key found in environment or CredentialsManager")
        return OpenRouter(id=model_id, api_key=api_key, **extra_kwargs)
    if provider in ("gemini", "google"):
        return Gemini(id=model_id, **extra_kwargs)
    if provider == "cerebras":
        return Cerebras(id=model_id, **extra_kwargs)

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

    # Set environment variable from CredentialsManager for Agno to use
    _set_api_key_env_var(provider)

    # Get extra kwargs if specified
    extra_kwargs = model_config.extra_kwargs or {}

    return _create_model_for_provider(provider, model_id, model_config, extra_kwargs)


def _build_full_prompt(prompt: str, thread_history: list[dict[str, Any]] | None = None) -> str:
    """Build full prompt with thread history context."""
    if not thread_history:
        return prompt

    context = "Previous conversation in this thread:\n"
    for msg in thread_history:
        context += f"{msg['sender']}: {msg['body']}\n"
    context += "\nCurrent message:\n"
    return context + prompt


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
) -> RunResponse:
    """Cached wrapper for agent.arun() calls."""
    cache = get_cache(storage_path)
    if cache is None:
        return await agent.arun(full_prompt, session_id=session_id)  # type: ignore[no-any-return]

    model = agent.model
    assert model is not None
    cache_key = _build_cache_key(agent, full_prompt, session_id)
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        logger.info("Cache hit", agent=agent_name)
        return cached_result  # type: ignore[no-any-return]

    response = await agent.arun(full_prompt, session_id=session_id)

    cache.set(cache_key, response)
    logger.info("Response cached", agent=agent_name)

    return response  # type: ignore[no-any-return]


async def _prepare_agent_and_prompt(
    agent_name: str,
    prompt: str,
    storage_path: Path,
    room_id: str | None,
    config: Config,
    thread_history: list[dict[str, Any]] | None = None,
) -> tuple[Agent, str]:
    """Prepare agent and full prompt for AI processing.

    Returns:
        Tuple of (agent, full_prompt, session_id)

    """
    enhanced_prompt = await build_memory_enhanced_prompt(prompt, agent_name, storage_path, config, room_id)
    full_prompt = _build_full_prompt(enhanced_prompt, thread_history)
    logger.info("Preparing agent and prompt", agent=agent_name, full_prompt=full_prompt)
    agent = create_agent(agent_name, config)
    return agent, full_prompt


async def ai_response(
    agent_name: str,
    prompt: str,
    session_id: str,
    storage_path: Path,
    config: Config,
    thread_history: list[dict[str, Any]] | None = None,
    room_id: str | None = None,
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
            config,
            thread_history,
        )
    except Exception as e:
        logger.exception("Error preparing agent", agent=agent_name)
        return get_user_friendly_error_message(e, agent_name)

    # Execute the AI call - this can fail for network, rate limits, etc.
    try:
        response = await _cached_agent_run(agent, full_prompt, session_id, agent_name, storage_path)
    except Exception as e:
        logger.exception("Error generating AI response", agent=agent_name)
        return get_user_friendly_error_message(e, agent_name)

    # Extract response content - this shouldn't fail
    return _extract_response_content(response)


async def stream_agent_response(  # noqa: C901, PLR0912
    agent_name: str,
    prompt: str,
    session_id: str,
    storage_path: Path,
    config: Config,
    thread_history: list[dict[str, Any]] | None = None,
    room_id: str | None = None,
) -> AsyncIterator[str]:
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

    Yields:
        Chunks of the AI response as they become available

    """
    logger.info("AI streaming request", agent=agent_name)

    # Prepare agent and prompt - this can fail if agent creation fails
    try:
        agent, full_prompt = await _prepare_agent_and_prompt(
            agent_name,
            prompt,
            storage_path,
            room_id,
            config,
            thread_history,
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
        stream_generator = await agent.arun(full_prompt, session_id=session_id, stream=True)
    except Exception as e:
        logger.exception("Error starting streaming AI response")
        yield get_user_friendly_error_message(e, agent_name)
        return

    # Process the stream events
    try:
        async for event in stream_generator:
            if isinstance(event, RunResponseContentEvent) and event.content:
                chunk_text = str(event.content)
                full_response += chunk_text
                yield chunk_text
            elif isinstance(event, ToolCallStartedEvent):
                tool_msg = _format_tool_started_message(event)
                if tool_msg:
                    full_response += tool_msg
                    yield tool_msg
            elif isinstance(event, ToolCallCompletedEvent):
                result_msg = _format_tool_completed_message(event)
                if result_msg:
                    full_response += result_msg
                    yield result_msg
            else:
                logger.warning(f"Unhandled event type: {type(event).__name__} - {event}")
    except Exception as e:
        logger.exception("Error during streaming AI response")
        yield get_user_friendly_error_message(e, agent_name)
        return

    if cache is not None and full_response:
        cached_response = RunResponse(content=full_response)
        cache.set(cache_key, cached_response)
        logger.info("Response cached", agent=agent_name)
