"""Memory configuration and setup."""

import os
from pathlib import Path
from typing import Any

from mem0 import Memory  # type: ignore[import-untyped]

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
    from ..agent_loader import load_config

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
        host = app_config.memory.embedder.config.host if hasattr(app_config.memory.embedder.config, "host") else None
        if not host:
            host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        embedder_config["config"]["ollama_base_url"] = host

    config = {
        "embedder": embedder_config,
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
    config = get_memory_config(storage_path)

    memory = Memory()
    memory.configure(config)

    logger.info(f"Created memory instance with ChromaDB at {storage_path}")
    return memory
