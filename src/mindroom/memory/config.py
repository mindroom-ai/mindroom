"""Memory configuration and setup."""

import os
from pathlib import Path
from typing import Any

from mem0 import Memory

from ..agent_config import load_config
from ..logging_config import get_logger

logger = get_logger(__name__)


def get_memory_config(storage_path: Path) -> dict:
    """Get Mem0 configuration with ChromaDB backend.

    Args:
        storage_path: Base directory for memory storage

    Returns:
        Configuration dictionary for Mem0
    """
    # Load configuration from config.yaml
    app_config = load_config()

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

    # Get LLM config from main models section
    default_model = app_config.models.get("default")
    if default_model:
        llm_provider = default_model.provider
        llm_id = default_model.id

        llm_config: dict[str, Any] = {
            "provider": llm_provider,
            "config": {
                "model": llm_id,
            },
        }

        # Add provider-specific LLM config
        if llm_provider == "ollama":
            llm_host = default_model.host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")
            llm_config["config"]["ollama_base_url"] = llm_host
            llm_config["config"]["temperature"] = 0.1
            llm_config["config"]["top_p"] = 1
        elif llm_provider == "openai":
            llm_config["config"]["api_key"] = os.environ.get("OPENAI_API_KEY")
            llm_config["config"]["temperature"] = 0
    else:
        # Fallback to Ollama if no model configured
        llm_config = {
            "provider": "ollama",
            "config": {
                "model": "llama3.2",
                "ollama_base_url": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                "temperature": 0.1,
                "top_p": 1,
            },
        }

    config = {
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

    return config


def create_memory_instance(storage_path: Path) -> Memory:
    """Create a Mem0 memory instance with ChromaDB backend.

    Args:
        storage_path: Base directory for memory storage

    Returns:
        Configured Memory instance
    """
    config_dict = get_memory_config(storage_path)

    # Create Memory instance with dictionary config directly
    # Mem0 expects a dict for configuration, not config objects
    memory = Memory.from_config(config_dict)

    logger.info(f"Created memory instance with ChromaDB at {storage_path}")
    return memory
