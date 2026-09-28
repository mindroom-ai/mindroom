"""Memory configuration and setup."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from mindroom.credentials_sync import get_api_key_for_provider, get_ollama_host
from mindroom.embedding_factory import resolve_embedder_settings
from mindroom.embeddings import effective_mem0_embedder_signature, ensure_sentence_transformers_dependencies
from mindroom.logging_config import get_logger
from mindroom.model_defaults import MEMORY_OLLAMA_LLM, OLLAMA_HOST_DEFAULT, OPENROUTER_BASE_URL_DEFAULT
from mindroom.timing import timed

if TYPE_CHECKING:
    from pathlib import Path

    from mem0 import AsyncMemory
    from mem0.llms.openai import OpenAILLM

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_MEMORY_COLLECTION_PREFIX = "mindroom_memories"


class _StrictOpenAIEmbedder(Protocol):
    def get_embedding(self, text: str) -> list[float]: ...

    def get_embeddings_batch(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class _Mem0StrictOpenAIEmbedder:
    """Adapt MindRoom's strict OpenAI embedder to Mem0's embedding interface."""

    embedder: _StrictOpenAIEmbedder
    _operation_failure: Exception | None = field(default=None, init=False, repr=False)

    def _remember_operation_failure(self, exc: Exception) -> None:
        if self._operation_failure is None:
            self._operation_failure = exc

    def begin_operation(self) -> None:
        """Start tracking failures that Mem0 may swallow during one operation."""
        self._operation_failure = None

    def raise_for_operation_failure(self) -> None:
        """Raise a safe error when Mem0 swallowed an embedding failure."""
        failure = self._operation_failure
        self._operation_failure = None
        if failure is not None:
            raise failure

    def embed(
        self,
        text: str,
        memory_action: Literal["add", "search", "update"] | None = None,
    ) -> list[float]:
        del memory_action
        try:
            return self.embedder.get_embedding(text.replace("\n", " "))
        except Exception as exc:
            self._remember_operation_failure(exc)
            raise

    def embed_batch(
        self,
        texts: list[str],
        memory_action: Literal["add", "search", "update"] = "add",
    ) -> list[list[float]]:
        del memory_action
        try:
            return self.embedder.get_embeddings_batch([text.replace("\n", " ") for text in texts])
        except Exception as exc:
            self._remember_operation_failure(exc)
            raise


def _chroma_similarity_from_distance(distance: float | None, space: str) -> float | None:
    """Convert one Chroma distance to the similarity score mem0 expects."""
    if distance is None:
        return None
    if space == "l2":
        # Chroma's l2 is squared L2; for unit-normalized embeddings d = 2 * (1 - cos).
        return 1.0 - distance / 2.0
    # Chroma's cosine distance = 1 - cos and ip distance = 1 - dot.
    return 1.0 - distance


def _install_chroma_similarity_scores(vector_store: object) -> None:
    """Make mem0's Chroma adapter report similarities instead of raw distances.

    mem0 2.x treats ``OutputData.score`` as a similarity everywhere (the search
    gate drops hits with ``score < threshold``, ranking sorts descending, and
    add-time dedup checks ``score >= 0.95``), but its Chroma adapter fills
    ``score`` with the raw Chroma distance. That inversion silently drops the
    closest matches, so near-verbatim memory queries return nothing.
    Rewriting the scores at the store boundary makes mem0's threshold and
    ranking semantics correct for Chroma-backed memory.
    """
    # Imported at first use to keep chromadb out of module import time.
    from mem0.vector_stores.chroma import ChromaDB, OutputData  # noqa: PLC0415

    if not isinstance(vector_store, ChromaDB):
        return
    original_search = vector_store.search
    collection_metadata = vector_store.collection.metadata or {}
    space = str(collection_metadata.get("hnsw:space", "l2"))

    def search_with_similarity_scores(
        query: str,
        vectors: list[list[float]],
        top_k: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[OutputData]:
        hits = original_search(query=query, vectors=vectors, top_k=top_k, filters=filters)
        for hit in hits:
            hit.score = _chroma_similarity_from_distance(hit.score, space)
        return hits

    vector_store.search = search_with_similarity_scores  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]


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


def _mem0_llm_config(config: Config, runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Build the Mem0 LLM config from ``memory.llm``, defaulting to local Ollama."""
    memory_llm = config.memory.llm
    if memory_llm is None:
        logger.warning(f"No memory LLM configured, using default ollama/{MEMORY_OLLAMA_LLM}")
        return {
            "provider": "ollama",
            "config": {
                "model": MEMORY_OLLAMA_LLM,
                "ollama_base_url": get_ollama_host(runtime_paths=runtime_paths) or OLLAMA_HOST_DEFAULT,
                "temperature": 0.1,
                "top_p": 1,
            },
        }

    llm_provider = memory_llm.provider
    llm_provider_config: dict[str, Any] = {}
    # Copy config but handle provider-specific field names
    for key, value in memory_llm.config.items():
        if key == "host" and llm_provider == "ollama":
            llm_provider_config["ollama_base_url"] = (
                get_ollama_host(runtime_paths=runtime_paths) or value or OLLAMA_HOST_DEFAULT
            )
        elif key != "host":  # Skip host for other fields
            llm_provider_config[key] = value

    if llm_provider in {"openai", "anthropic", "openrouter"}:
        api_key = get_api_key_for_provider(llm_provider, runtime_paths=runtime_paths)
        if api_key:
            llm_provider_config["api_key"] = api_key

    if llm_provider == "openrouter":
        # Mem0's OpenAI LLM reads openrouter_base_url instead when OPENROUTER_API_KEY is exported.
        llm_provider_config.setdefault("openai_base_url", OPENROUTER_BASE_URL_DEFAULT)
        llm_provider_config.setdefault("openrouter_base_url", OPENROUTER_BASE_URL_DEFAULT)

    logger.info("Configured memory LLM", provider=llm_provider, model=memory_llm.config.get("model"))
    return {
        # Mem0 has no OpenRouter provider; its OpenAI LLM speaks the same API.
        "provider": "openai" if llm_provider == "openrouter" else llm_provider,
        "config": llm_provider_config,
    }


def _get_memory_config(storage_path: Path, config: Config, runtime_paths: RuntimePaths) -> dict:
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
    resolved_embedder = resolve_embedder_settings(app_config, runtime_paths)
    embedder_provider = resolved_embedder.provider

    # Build embedder config from config.yaml
    embedder_provider_config: dict[str, Any] = {
        "model": resolved_embedder.model,
    }
    embedder_config: dict[str, Any] = {
        "provider": "huggingface" if embedder_provider == "sentence_transformers" else embedder_provider,
        "config": embedder_provider_config,
    }

    # Add provider-specific configuration
    if embedder_provider == "openai":
        embedder_provider_config["api_key"] = resolved_embedder.api_key
        # Support custom OpenAI-compatible base URL (e.g., llama.cpp)
        if resolved_embedder.host:
            embedder_provider_config["openai_base_url"] = resolved_embedder.host
        if resolved_embedder.dimensions is not None:
            embedder_provider_config["embedding_dims"] = resolved_embedder.dimensions
    elif embedder_provider == "ollama":
        embedder_provider_config["ollama_base_url"] = resolved_embedder.host
    elif embedder_provider == "sentence_transformers" and resolved_embedder.dimensions is not None:
        embedder_provider_config["embedding_dims"] = resolved_embedder.dimensions

    return {
        "embedder": embedder_config,
        "llm": _mem0_llm_config(app_config, runtime_paths),
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
) -> AsyncMemory:
    """Create a Mem0 memory instance with ChromaDB backend.

    Args:
        storage_path: Base directory for memory storage
        config: Application configuration
        runtime_paths: Explicit runtime context for credential-backed provider settings.

    Returns:
        Configured AsyncMemory instance

    """
    from mem0 import AsyncMemory  # noqa: PLC0415

    config_dict = _get_memory_config(storage_path, config, runtime_paths)
    if config.memory.embedder.provider == "sentence_transformers":
        ensure_sentence_transformers_dependencies(runtime_paths)

    # Create AsyncMemory instance with dictionary config directly
    # Mem0 expects a dict for configuration, not config objects
    memory = AsyncMemory.from_config(config_dict)
    memory_llm = config.memory.llm
    if memory_llm is not None and memory_llm.provider == "openai":
        from mindroom.memory._mem0_openai import install_mem0_openai_compatibility  # noqa: PLC0415

        install_mem0_openai_compatibility(cast("OpenAILLM", memory.llm))
    if config.memory.embedder.provider == "openai":
        # Mem0's own OpenAI embedder indexes response.data[0] without validation
        # and can expose raw provider errors. Reuse MindRoom's strict boundary.
        from mindroom.embedding_factory import create_configured_embedder  # noqa: PLC0415

        strict_embedder = cast("_StrictOpenAIEmbedder", create_configured_embedder(config, runtime_paths))
        cast("Any", memory).embedding_model = _Mem0StrictOpenAIEmbedder(strict_embedder)
    _install_chroma_similarity_scores(memory.vector_store)

    logger.info("created_memory_instance", path=config_dict["vector_store"]["config"]["path"])
    return memory
