import os
from pathlib import Path
from typing import Any

import diskcache
from agno.models.anthropic import Claude
from agno.models.base import Model
from agno.models.ollama import Ollama
from agno.models.openai import OpenAIChat
from agno.run.response import RunResponse
from dotenv import load_dotenv
from loguru import logger

from .agent_loader import create_agent

# Load environment variables from .env file
load_dotenv()

# Load configuration from .env file
AGNO_MODEL_STR = os.getenv("AGNO_MODEL")

# Configure caching
ENABLE_CACHE = os.getenv("ENABLE_AI_CACHE", "true").lower() == "true"


def get_cache(storage_path: Path) -> diskcache.Cache | None:
    """Get or create a cache instance for the given storage path."""
    if not ENABLE_CACHE:
        return None
    return diskcache.Cache(storage_path / ".ai_cache")


def get_model_instance() -> Model:
    """Parses the AGNO_MODEL string and returns an instantiated model."""
    if not AGNO_MODEL_STR:
        msg = "AGNO_MODEL is not configured in the .env file."
        raise ValueError(msg)

    provider, model_id = AGNO_MODEL_STR.split(":", 1)
    logger.info(f"Using AI model from provider '{provider}' with ID '{model_id}'")

    if provider == "ollama":
        return Ollama(id=model_id, host=os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    if provider == "openai":
        return OpenAIChat(id=model_id)
    if provider == "anthropic":
        return Claude(id=model_id)
    msg = f"Unsupported AI provider: {provider}"
    raise ValueError(msg)


async def _cached_agent_run(
    agent_name: str,
    prompt: str,
    session_id: str,
    model: Model,
    storage_path: Path,
    thread_history: list[dict[str, Any]] | None = None,
) -> RunResponse:
    """Cached wrapper for agent.arun() calls."""
    # Format thread history into context
    full_prompt = prompt
    if thread_history:
        context = "Previous conversation in this thread:\n"
        for msg in thread_history:
            # Include all messages for full context
            context += f"{msg['sender']}: {msg['body']}\n"
        context += "\nCurrent message:\n"
        full_prompt = context + prompt

    cache = get_cache(storage_path)
    if cache is None:
        # If caching is disabled, run directly
        agent = create_agent(agent_name, model, storage_path)
        return await agent.arun(full_prompt, session_id=session_id)  # type: ignore[no-any-return]

    # Create a cache key based on agent name, prompt, and model
    cache_key = f"{agent_name}:{model.__class__.__name__}:{model.id}:{full_prompt}:{session_id}"

    # Check if result exists in cache
    cached_result = cache.get(cache_key)
    if cached_result is not None:
        logger.info(f"Cache hit for agent '{agent_name}' with prompt: '{prompt}'")
        return cached_result  # type: ignore[no-any-return]

    # If not in cache, run the agent and cache the result
    agent = create_agent(agent_name, model, storage_path=storage_path)
    response = await agent.arun(full_prompt, session_id=session_id)

    # Cache the result
    cache.set(cache_key, response)
    logger.info(f"Cached response for agent '{agent_name}' with prompt: '{prompt}'")

    return response  # type: ignore[no-any-return]


async def ai_response(
    agent_name: str,
    prompt: str,
    session_id: str,
    storage_path: Path,
    thread_history: list[dict[str, Any]] | None = None,
) -> str:
    """Generates a response using the specified agno Agent."""
    logger.info(f"Routing to agent '{agent_name}' for prompt: '{prompt}'")
    try:
        model = get_model_instance()
        response = await _cached_agent_run(agent_name, prompt, session_id, model, storage_path, thread_history)
        return response.content or ""
    except Exception as e:
        logger.exception(f"Error generating AI response: {e}")
        return f"Sorry, I encountered an error trying to generate a response: {e}"
