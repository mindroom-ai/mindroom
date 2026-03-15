"""Memory configuration and setup."""

import hashlib
from pathlib import Path
from typing import Any

from mem0 import AsyncMemory

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.embeddings import effective_mem0_embedder_signature, ensure_sentence_transformers_dependencies
from mindroom.logging_config import get_logger

logger = get_logger(__name__)
_MEMORY_COLLECTION_PREFIX = "mindroom_memories"


def _memory_collection_name(config: Config) -> str:
    """Return a stable Chroma collection name for the active embedder settings."""
    embedder = config.memory.embedder
    embedder_config = embedder.config
    signature = "|".join(
        effective_mem0_embedder_signature(
            embedder.provider,
            embedder_config.model,
            host=embedder_config.host,
            dimensions=embedder_config.dimensions,
        ),
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:8]
    return f"{_MEMORY_COLLECTION_PREFIX}_{digest}"


def _get_memory_config(storage_path: Path, config: Config, runtime_paths: RuntimePaths) -> dict:  # noqa: C901, PLR0912
    """Get Mem0 configuration with ChromaDB backend.

    Args:
        storage_path: Base directory for memory storage
        config: Application configuration
        runtime_paths: Explicit runtime context for credential-backed provider settings.

    Returns:
        Configuration dictionary for Mem0

    """
    app_config = config
    # Canonicalize once so Chroma path is independent of runtime cwd changes.
    resolved_storage_path = storage_path.expanduser().resolve()
    creds_manager = get_runtime_shared_credentials_manager(runtime_paths)

    # Ensure storage directories exist
    chroma_path = resolved_storage_path / "chroma"
    chroma_path.mkdir(parents=True, exist_ok=True)
    embedder_provider = app_config.memory.embedder.provider

    # Build embedder config from config.yaml
    embedder_config: dict[str, Any] = {
        "provider": "huggingface" if embedder_provider == "sentence_transformers" else embedder_provider,
        "config": {
            "model": app_config.memory.embedder.config.model,
        },
    }

    # Add provider-specific configuration
    if embedder_provider == "openai":
        api_key = creds_manager.get_api_key("openai")
        if api_key:
            embedder_config["config"]["api_key"] = api_key
        # Support custom OpenAI-compatible base URL (e.g., llama.cpp)
        if app_config.memory.embedder.config.host:
            embedder_config["config"]["openai_base_url"] = app_config.memory.embedder.config.host
        if app_config.memory.embedder.config.dimensions is not None:
            embedder_config["config"]["embedding_dims"] = app_config.memory.embedder.config.dimensions
    elif embedder_provider == "ollama":
        # Check CredentialsManager for Ollama host
        ollama_creds = creds_manager.load_credentials("ollama")
        if ollama_creds and "host" in ollama_creds:
            host = ollama_creds["host"]
        else:
            host = app_config.memory.embedder.config.host or "http://localhost:11434"
        embedder_config["config"]["ollama_base_url"] = host
    elif embedder_provider == "sentence_transformers" and app_config.memory.embedder.config.dimensions is not None:
        embedder_config["config"]["embedding_dims"] = app_config.memory.embedder.config.dimensions

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

        if app_config.memory.llm.provider == "openai":
            api_key = creds_manager.get_api_key("openai")
            if api_key:
                llm_config["config"]["api_key"] = api_key
        elif app_config.memory.llm.provider == "anthropic":
            api_key = creds_manager.get_api_key("anthropic")
            if api_key:
                llm_config["config"]["api_key"] = api_key

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
                "collection_name": _memory_collection_name(app_config),
                "path": str(chroma_path),
            },
        },
    }


async def create_memory_instance(storage_path: Path, config: Config, runtime_paths: RuntimePaths) -> AsyncMemory:
    """Create a Mem0 memory instance with ChromaDB backend.

    Args:
        storage_path: Base directory for memory storage
        config: Application configuration
        runtime_paths: Explicit runtime context for credential-backed provider settings.

    Returns:
        Configured AsyncMemory instance

    """
    config_dict = _get_memory_config(storage_path, config, runtime_paths)
    if config.memory.embedder.provider == "sentence_transformers":
        ensure_sentence_transformers_dependencies(runtime_paths)

    # Create AsyncMemory instance with dictionary config directly
    # Mem0 expects a dict for configuration, not config objects
    memory = await AsyncMemory.from_config(config_dict)

    logger.info(f"Created memory instance with ChromaDB at {config_dict['vector_store']['config']['path']}")
    return memory
