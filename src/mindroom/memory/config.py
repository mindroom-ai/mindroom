"""Memory configuration and setup."""

import os
from pathlib import Path

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
    # Ensure storage directories exist
    chroma_path = storage_path / "chroma"
    chroma_path.mkdir(parents=True, exist_ok=True)

    config = {
        "embedder": {
            "provider": "openai",
            "config": {
                "model": "text-embedding-3-small",
                "api_key": os.environ.get("OPENAI_API_KEY"),
            },
        },
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
