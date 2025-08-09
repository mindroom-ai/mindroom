"""Memory configuration and setup."""

import os
from pathlib import Path
from typing import Any

from mem0 import AsyncMemory

from ..logging_config import get_logger
from ..models import Config

logger = get_logger(__name__)


def get_memory_config(storage_path: Path, config: Config) -> dict:
    """Get Mem0 configuration with ChromaDB backend.

    Args:
        storage_path: Base directory for memory storage
        config: Application configuration

    Returns:
        Configuration dictionary for Mem0
    """
    app_config = config

    # Ensure storage directories exist
    chroma_path = storage_path / "chroma"
    chroma_path.mkdir(parents=True, exist_ok=True)

    # Build embedder config from config.yaml
    embedder_config: dict[str, Any] = {
        "provider": app_config.memory.embedder.provider,
        "config": {
            "model": app_config.memory.embedder.config.model,
        },
    }

    # Add provider-specific configuration
    if app_config.memory.embedder.provider == "openai":
        embedder_config["config"]["api_key"] = os.environ.get("OPENAI_API_KEY")
    elif app_config.memory.embedder.provider == "ollama":
        # Add Ollama host if specified
        host = app_config.memory.embedder.config.host
        if not host:
            host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        embedder_config["config"]["ollama_base_url"] = host

    # Build LLM config from memory configuration
    if app_config.memory.llm:
        llm_config: dict[str, Any] = {
            "provider": app_config.memory.llm.provider,
            "config": {},
        }

        # Copy config but handle provider-specific field names
        for key, value in app_config.memory.llm.config.items():
            if key == "host" and app_config.memory.llm.provider == "ollama":
                # mem0 expects ollama_base_url, not host
                llm_config["config"]["ollama_base_url"] = value or os.environ.get(
                    "OLLAMA_HOST", "http://localhost:11434"
                )
            elif key != "host":  # Skip host for other fields
                llm_config["config"][key] = value

        # Add API keys for providers that need them
        if app_config.memory.llm.provider == "openai":
            llm_config["config"]["api_key"] = os.environ.get("OPENAI_API_KEY")
        elif app_config.memory.llm.provider == "anthropic":
            llm_config["config"]["api_key"] = os.environ.get("ANTHROPIC_API_KEY")

        logger.info(
            f"Using {app_config.memory.llm.provider} model '{app_config.memory.llm.config.get('model')}' for memory"
        )
    else:
        # Fallback if no LLM configured
        logger.warning("No memory LLM configured, using default ollama/llama3.2")
        llm_config = {
            "provider": "ollama",
            "config": {
                "model": "llama3.2",
                "ollama_base_url": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                "temperature": 0.1,
                "top_p": 1,
            },
        }

    memory_config = {
        "embedder": embedder_config,
        "llm": llm_config,
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": "mindroom_memories",
                "path": str(chroma_path),
            },
        },
    }

    return memory_config


async def create_memory_instance(storage_path: Path, config: Config) -> AsyncMemory:
    """Create a Mem0 memory instance with ChromaDB backend.

    Args:
        storage_path: Base directory for memory storage
        config: Application configuration

    Returns:
        Configured AsyncMemory instance
    """
    config_dict = get_memory_config(storage_path, config)

    # Create AsyncMemory instance with dictionary config directly
    # Mem0 expects a dict for configuration, not config objects
    memory = await AsyncMemory.from_config(config_dict)

    logger.info(f"Created memory instance with ChromaDB at {storage_path}")
    return memory
