"""Memory configuration and setup."""

import hashlib
from pathlib import Path
from typing import Any

from mem0 import AsyncMemory

from mindroom.config.main import Config
from mindroom.connections import ConnectionPurpose, connection_api_key, resolve_connection
from mindroom.constants import RuntimePaths
from mindroom.credentials_sync import get_ollama_host
from mindroom.embeddings import effective_mem0_embedder_signature, ensure_sentence_transformers_dependencies
from mindroom.logging_config import get_logger
from mindroom.timing import timed

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


def _resolved_connection_api_key(
    config: Config,
    *,
    provider: str,
    purpose: ConnectionPurpose,
    connection_name: str | None,
    runtime_paths: RuntimePaths,
) -> str | None:
    connection = resolve_connection(
        config,
        provider=provider,
        purpose=purpose,
        connection_name=connection_name,
        runtime_paths=runtime_paths,
    )
    return connection_api_key(connection)


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
        api_key = _resolved_connection_api_key(
            app_config,
            provider=embedder_provider,
            purpose="embedder",
            connection_name=app_config.memory.embedder.config.connection,
            runtime_paths=runtime_paths,
        )
        if api_key:
            embedder_config["config"]["api_key"] = api_key
        # Support custom OpenAI-compatible base URL (e.g., llama.cpp)
        if app_config.memory.embedder.config.host:
            embedder_config["config"]["openai_base_url"] = app_config.memory.embedder.config.host
        if app_config.memory.embedder.config.dimensions is not None:
            embedder_config["config"]["embedding_dims"] = app_config.memory.embedder.config.dimensions
    elif embedder_provider == "ollama":
        host = app_config.memory.embedder.config.host or get_ollama_host(runtime_paths) or "http://localhost:11434"
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
                llm_config["config"]["ollama_base_url"] = (
                    value or get_ollama_host(runtime_paths) or "http://localhost:11434"
                )
            elif key != "host":  # Skip host for other fields
                llm_config["config"][key] = value

        if app_config.memory.llm.provider in {"openai", "anthropic"}:
            api_key = _resolved_connection_api_key(
                app_config,
                provider=app_config.memory.llm.provider,
                purpose="memory_llm",
                connection_name=app_config.memory.llm.connection,
                runtime_paths=runtime_paths,
            )
            if api_key:
                llm_config["config"]["api_key"] = api_key

        logger.info(
            "Configured memory LLM",
            provider=app_config.memory.llm.provider,
            model=app_config.memory.llm.config.get("model"),
        )
    else:
        # Fallback if no LLM configured
        logger.warning("No memory LLM configured, using default ollama/llama3.2")
        ollama_host = get_ollama_host(runtime_paths) or "http://localhost:11434"

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


@timed("system_prompt_assembly.memory_search.mem0.async_memory_from_config")
async def create_memory_instance(
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    timing_scope: str | None = None,
) -> AsyncMemory:
    """Create a Mem0 memory instance with ChromaDB backend.

    Args:
        storage_path: Base directory for memory storage
        config: Application configuration
        runtime_paths: Explicit runtime context for credential-backed provider settings.
        timing_scope: Optional correlated timing scope id for nested logs.

    Returns:
        Configured AsyncMemory instance

    """
    del timing_scope
    config_dict = _get_memory_config(storage_path, config, runtime_paths)
    if config.memory.embedder.provider == "sentence_transformers":
        ensure_sentence_transformers_dependencies(runtime_paths)

    # Create AsyncMemory instance with dictionary config directly
    # Mem0 expects a dict for configuration, not config objects
    memory = await AsyncMemory.from_config(config_dict)

    logger.info("created_memory_instance", path=config_dict["vector_store"]["config"]["path"])
    return memory
