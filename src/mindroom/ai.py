import functools
import os
import traceback
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import diskcache
from agno.agent import Agent
from agno.models.anthropic import Claude
from agno.models.base import Model
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.models.openrouter import OpenRouter
from agno.run.response import RunResponse, RunResponseContentEvent
from dotenv import load_dotenv

from .agent_config import load_config
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
    if provider == "openrouter":
        return OpenRouter(id=model_id)

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


def _build_cache_key(agent: Agent, full_prompt: str, session_id: str) -> str:
    model = agent.model
    assert model is not None, "Agent should always have a model in our implementation"
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
    assert model is not None, "Agent should always have a model in our implementation"
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
    thread_history: list[dict[str, Any]] | None = None,
) -> tuple[Agent, str]:
    """Prepare agent and full prompt for AI processing.

    Returns:
        Tuple of (agent, full_prompt, session_id)
    """
    from .agent_config import create_agent

    enhanced_prompt = await build_memory_enhanced_prompt(prompt, agent_name, storage_path, room_id)
    full_prompt = _build_full_prompt(enhanced_prompt, thread_history)
    logger.info("Preparing agent and prompt", agent=agent_name, full_prompt=full_prompt)
    agent = create_agent(agent_name, storage_path=storage_path)
    return agent, full_prompt


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
        agent, full_prompt = await _prepare_agent_and_prompt(agent_name, prompt, storage_path, room_id, thread_history)

        response = await _cached_agent_run(agent, full_prompt, session_id, agent_name, storage_path)
        response_text = response.content or ""
        await store_conversation_memory(prompt, agent_name, storage_path, session_id, room_id)

        return response_text
    except Exception as e:
        # AI models can fail for various reasons (network, API limits, etc)
        logger.exception(f"Error generating AI response for agent {agent_name}: {e}")
        logger.error(f"Full error details - Type: {type(e).__name__}, Agent: {agent_name}, Storage: {storage_path}")
        logger.error(f"Session ID: {session_id}, Thread history length: {len(thread_history) if thread_history else 0}")
        logger.error(f"Traceback:\n{traceback.format_exc()}")
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

    agent, full_prompt = await _prepare_agent_and_prompt(agent_name, prompt, storage_path, room_id, thread_history)

    cache = get_cache(storage_path)
    if cache is not None:
        model = agent.model
        assert model is not None, "Agent should always have a model in our implementation"
        cache_key = _build_cache_key(agent, full_prompt, session_id)
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            logger.info("Cache hit", agent=agent_name)
            response_text = cached_result.content or ""
            yield response_text
            await store_conversation_memory(prompt, agent_name, storage_path, session_id, room_id)
            return

    full_response = ""

    try:
        stream_generator = await agent.arun(full_prompt, session_id=session_id, stream=True)
        async for event in stream_generator:
            if isinstance(event, RunResponseContentEvent) and event.content:
                chunk_text = str(event.content)
                full_response += chunk_text
                yield chunk_text

    except Exception as e:
        logger.exception(f"Error generating streaming AI response: {e}")
        error_message = f"Sorry, I encountered an error trying to generate a response: {e}"
        yield error_message
        return

    if cache is not None and full_response:
        cached_response = RunResponse(content=full_response)
        cache.set(cache_key, cached_response)
        logger.info("Response cached", agent=agent_name)

    if full_response:
        await store_conversation_memory(prompt, agent_name, storage_path, session_id, room_id)
