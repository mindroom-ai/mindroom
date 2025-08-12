"""Memory configuration and setup."""

import os
from pathlib import Path
from typing import Any

from mem0 import AsyncMemory

from mindroom.config import Config
from mindroom.credentials import get_credentials_manager
from mindroom.logging_config import get_logger

logger = get_logger(__name__)


def get_memory_config(storage_path: Path, config: Config) -> dict:  # noqa: C901, PLR0912
    """Get Mem0 configuration with ChromaDB backend.

    Args:
        storage_path: Base directory for memory storage
        config: Application configuration

    Returns:
        Configuration dictionary for Mem0

    """
    app_config = config
    creds_manager = get_credentials_manager()

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
        # Set environment variable from CredentialsManager for Mem0 to use
        api_key = creds_manager.get_api_key("openai")
        if api_key:
            os.environ["OPENAI_API_KEY"] = api_key
    elif app_config.memory.embedder.provider == "ollama":
        # Check CredentialsManager for Ollama host
        ollama_creds = creds_manager.load_credentials("ollama")
        if ollama_creds and "host" in ollama_creds:
            host = ollama_creds["host"]
        else:
            host = app_config.memory.embedder.config.host or "http://localhost:11434"
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
                # Check CredentialsManager for Ollama host
                ollama_creds = creds_manager.load_credentials("ollama")
                if ollama_creds and "host" in ollama_creds:
                    llm_config["config"]["ollama_base_url"] = ollama_creds["host"]
                else:
                    llm_config["config"]["ollama_base_url"] = value or "http://localhost:11434"
            elif key != "host":  # Skip host for other fields
                llm_config["config"][key] = value

        # Set environment variables from CredentialsManager for Mem0 to use
        if app_config.memory.llm.provider == "openai":
            api_key = creds_manager.get_api_key("openai")
            if api_key:
                os.environ["OPENAI_API_KEY"] = api_key
        elif app_config.memory.llm.provider == "anthropic":
            api_key = creds_manager.get_api_key("anthropic")
            if api_key:
                os.environ["ANTHROPIC_API_KEY"] = api_key

        logger.info(
            f"Using {app_config.memory.llm.provider} model '{app_config.memory.llm.config.get('model')}' for memory",
        )
    else:
        # Fallback if no LLM configured
        logger.warning("No memory LLM configured, using default ollama/llama3.2")
        # Check CredentialsManager for Ollama host
        ollama_creds = creds_manager.load_credentials("ollama")
        ollama_host = ollama_creds["host"] if ollama_creds and "host" in ollama_creds else "http://localhost:11434"

        llm_config = {
            "provider": "ollama",
            "config": {
                "model": "llama3.2",
                "ollama_base_url": ollama_host,
                "temperature": 0.1,
                "top_p": 1,
            },
        }

    return {
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
