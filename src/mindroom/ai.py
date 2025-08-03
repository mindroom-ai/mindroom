import functools
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import diskcache
from agno.agent import Agent
from agno.models.anthropic import Claude
from agno.models.base import Model
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.run.response import RunResponse
from dotenv import load_dotenv

from .agent_config import create_agent, load_config
from .logging_config import get_logger
from .memory import (
    build_memory_enhanced_prompt,
    store_conversation_memory,
)

logger = get_logger(__name__)

load_dotenv()
ENABLE_CACHE = os.getenv("ENABLE_AI_CACHE", "true").lower() == "true"


@functools.cache
def get_cache(storage_path: Path) -> diskcache.Cache | None:
    """Get or create a cache instance for the given storage path."""
    return diskcache.Cache(storage_path / ".ai_cache") if ENABLE_CACHE else None


def get_model_instance(model_name: str = "default") -> Model:
    """Get a model instance from config.yaml.

    Args:
        model_name: Name of the model configuration to use (default: "default")

    Returns:
        Instantiated model

    Raises:
        ValueError: If model not found or provider not supported
    """
    config = load_config()

    if model_name not in config.models:
        available = ", ".join(sorted(config.models.keys()))
        msg = f"Unknown model: {model_name}. Available models: {available}"
        raise ValueError(msg)

    model_config = config.models[model_name]
    provider = model_config.provider
    model_id = model_config.id

    logger.info("Using AI model", model=model_name, provider=provider, id=model_id)

    if provider == "ollama":
        host = model_config.host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
        return Ollama(id=model_id, host=host)
    if provider == "openai":
        return OpenAIChat(id=model_id)
    if provider == "anthropic":
        return Claude(id=model_id)

    msg = f"Unsupported AI provider: {provider}"
    raise ValueError(msg)


def _build_full_prompt(prompt: str, thread_history: list[dict[str, Any]] | None = None) -> str:
    """Build full prompt with thread history context."""
    if not thread_history:
        return prompt

    context = "Previous conversation in this thread:\n"
    for msg in thread_history:
        context += f"{msg['sender']}: {msg['body']}\n"
    context += "\nCurrent message:\n"
    return context + prompt


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

    # Use agent's model for cache key
    model = agent.model
    assert model is not None, "Agent should always have a model in our implementation"
    cache_key = f"{agent_name}:{model.__class__.__name__}:{model.id}:{full_prompt}:{session_id}"
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
    thread_history: list[dict[str, Any]] | None = None,
) -> tuple[Agent, str, str]:
    """Prepare agent and full prompt for AI processing.

    Returns:
        Tuple of (agent, full_prompt, session_id)
    """
    model = get_model_instance()
    enhanced_prompt = build_memory_enhanced_prompt(prompt, agent_name, storage_path, room_id)
    full_prompt = _build_full_prompt(enhanced_prompt, thread_history)
    agent = create_agent(agent_name, model, storage_path=storage_path)
    return agent, full_prompt, enhanced_prompt


async def ai_response(
    agent_name: str,
    prompt: str,
    session_id: str,
    storage_path: Path,
    thread_history: list[dict[str, Any]] | None = None,
    room_id: str | None = None,
) -> str:
    """Generates a response using the specified agno Agent with memory integration.

    Args:
        agent_name: Name of the agent to use
        prompt: User prompt
        session_id: Session ID for conversation tracking
        storage_path: Path for storing agent data
        thread_history: Optional thread history
        room_id: Optional room ID for room memory access

    Returns:
        Agent response string
    """
    logger.info("AI request", agent=agent_name)
    try:
        agent, full_prompt, enhanced_prompt = await _prepare_agent_and_prompt(
            agent_name, prompt, storage_path, room_id, thread_history
        )

        response = await _cached_agent_run(agent, full_prompt, session_id, agent_name, storage_path)
        response_text = response.content or ""
        store_conversation_memory(prompt, response_text, agent_name, storage_path, session_id, room_id)

        return response_text
    except Exception as e:
        # AI models can fail for various reasons (network, API limits, etc)
        logger.exception(f"Error generating AI response: {e}")
        return f"Sorry, I encountered an error trying to generate a response: {e}"


async def ai_response_streaming(
    agent_name: str,
    prompt: str,
    session_id: str,
    storage_path: Path,
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
        thread_history: Optional thread history
        room_id: Optional room ID for room memory access

    Yields:
        Chunks of the AI response as they become available
    """
    logger.info("AI streaming request", agent=agent_name)

    # Prepare agent and prompt - these are deterministic operations
    agent, full_prompt, enhanced_prompt = await _prepare_agent_and_prompt(
        agent_name, prompt, storage_path, room_id, thread_history
    )

    # Check cache first - also deterministic
    cache = get_cache(storage_path)
    if cache is not None:
        model = agent.model
        assert model is not None, "Agent should always have a model in our implementation"
        cache_key = f"{agent_name}:{model.__class__.__name__}:{model.id}:{full_prompt}:{session_id}"
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            logger.info("Cache hit", agent=agent_name)
            # Yield cached response immediately (non-streaming)
            response_text = cached_result.content or ""
            yield response_text
            # Store in memory even for cache hits
            store_conversation_memory(prompt, response_text, agent_name, storage_path, session_id, room_id)
            return

    # No cache hit - use streaming
    from agno.run.response import RunResponseContentEvent

    full_response = ""

    try:
        stream_generator = await agent.arun(full_prompt, session_id=session_id, stream=True)
        async for event in stream_generator:
            # We're only interested in content events for streaming
            if isinstance(event, RunResponseContentEvent) and event.content:
                chunk_text = str(event.content)
                full_response += chunk_text
                yield chunk_text

    except Exception as e:
        logger.exception(f"Error generating streaming AI response: {e}")
        error_message = f"Sorry, I encountered an error trying to generate a response: {e}"
        yield error_message
        return

    # Cache the complete response - deterministic operation
    if cache is not None and full_response:
        # Create a mock response object to cache
        from agno.run.response import RunResponse

        cached_response = RunResponse(content=full_response)
        cache.set(cache_key, cached_response)
        logger.info("Response cached", agent=agent_name)

    # Store the complete response in memory - also deterministic
    if full_response:
        store_conversation_memory(prompt, full_response, agent_name, storage_path, session_id, room_id)
