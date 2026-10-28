"""Memory configuration and setup."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.credentials.sync import get_api_key_for_provider, get_ollama_host
from mindroom.embeddings.errors import embedder_request_error
from mindroom.embeddings.factory import resolve_embedder_settings
from mindroom.embeddings.signatures import effective_mem0_embedder_signature, ensure_sentence_transformers_dependencies
from mindroom.logging_config import get_logger
from mindroom.memory._mem0_usage import install_mem0_usage_capture
from mindroom.model_defaults import (
    MEMORY_OLLAMA_LLM,
    OLLAMA_HOST_DEFAULT,
    OPENROUTER_BASE_URL_DEFAULT,
    claude_rejects_sampling_controls,
    openai_rejects_sampling_controls,
)
from mindroom.timing import timed
from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment

if TYPE_CHECKING:
    from pathlib import Path

    from mem0 import AsyncMemory

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.embeddings.base import Embedder, EmbeddingPurpose
    from mindroom.llm.errors import ProviderError

logger = get_logger(__name__)
_MEMORY_COLLECTION_PREFIX = "mindroom_memories"
# Chroma publishes each store's shared system before starting it, so a build that overlaps the first build of the same
# store can reach the unstarted system, and both builds fail. Builds of one store take turns; Chroma keys that shared
# state by store path, so different stores still build in parallel.
_STORE_BUILD_LOCKS: dict[str, threading.Lock] = {}


@dataclass
class _Mem0StrictOpenAIEmbedder:
    """Adapt MindRoom's strict OpenAI embedder to Mem0's embedding interface."""

    embedder: Embedder
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
        return self.embed_batch([text], memory_action)[0]

    def embed_batch(
        self,
        texts: list[str],
        memory_action: Literal["add", "search", "update"] | None = "add",
    ) -> list[list[float]]:
        # Stored memories are documents; anything else, including the entities of a search query, is a query.
        purpose: EmbeddingPurpose = "document" if memory_action in {"add", "update"} else "query"
        try:
            return self.embedder.embed([text.replace("\n", " ") for text in texts], purpose=purpose)
        except Exception as exc:
            self._remember_operation_failure(exc)
            raise


def _ollama_failure(error: Exception) -> ProviderError | None:
    """Return the typed provider error of an Ollama SDK or transport failure, as MindRoom's Ollama embedder maps it."""
    from mindroom.providers.ollama.errors import provider_error  # noqa: PLC0415 - loads the Ollama SDK

    return provider_error(error, provider="ollama")


@dataclass(frozen=True)
class _Mem0ClassifiedOllamaEmbedder:
    """Raise the failures of Mem0's own Ollama embedder as the typed embedder errors MindRoom's embedders raise.

    It sends what Mem0's embedder sends, so stored memories keep matching their searches.
    """

    embedder: Any

    def embed(self, text: str, memory_action: Literal["add", "search", "update"] | None = None) -> list[float]:
        try:
            return self.embedder.embed(text, memory_action)
        except Exception as exc:
            raise embedder_request_error(exc, _ollama_failure(exc)) from exc

    def embed_batch(
        self,
        texts: list[str],
        memory_action: Literal["add", "search", "update"] | None = "add",
    ) -> list[list[float]]:
        try:
            return self.embedder.embed_batch(texts, memory_action)
        except Exception as exc:
            raise embedder_request_error(exc, _ollama_failure(exc)) from exc


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
            query_prefix=embedder_config.query_prefix,
            document_prefix=embedder_config.document_prefix,
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

    if llm_provider in {"openai", "anthropic", "openrouter"} and not llm_provider_config.get("api_key"):
        api_key = get_api_key_for_provider(llm_provider, runtime_paths=runtime_paths)
        if api_key:
            llm_provider_config["api_key"] = api_key

    model = llm_provider_config.get("model")
    if llm_provider == "anthropic" and isinstance(model, str) and claude_rejects_sampling_controls(model):
        # Mem0's Anthropic LLM sends its default temperature of 0.1, else a set top_p, and never top_k; these models
        # reject every sampling value, and Mem0 sends neither when both are None.
        llm_provider_config["temperature"] = None
        llm_provider_config["top_p"] = None

    if llm_provider == "openrouter":
        # Mem0's OpenAI LLM reads openrouter_base_url instead when OPENROUTER_API_KEY is exported.
        llm_provider_config.setdefault("openai_base_url", OPENROUTER_BASE_URL_DEFAULT)
        llm_provider_config.setdefault("openrouter_base_url", OPENROUTER_BASE_URL_DEFAULT)

    logger.info("Configured memory LLM", provider=llm_provider, model=model)
    return {
        # Mem0 has no OpenRouter provider; its OpenAI LLM speaks the same API.
        "provider": "openai" if llm_provider == "openrouter" else llm_provider,
        "config": llm_provider_config,
    }


def _get_memory_config(storage_path: Path, config: Config, runtime_paths: RuntimePaths) -> dict:
    """Get Mem0 configuration with a ChromaDB backend and history database under ``storage_path``.

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
    # Mem0's history database keeps the text of changed and deleted memories, and the latest messages it adds to the
    # next extraction prompt under keys that do not name the requester, so each storage root needs its own.
    history_dir = resolved_storage_path / "mem0"
    history_dir.mkdir(exist_ok=True)
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
        "history_db_path": str(history_dir / "history.db"),
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
    config_dict = _get_memory_config(storage_path, config, runtime_paths)
    store_path = config_dict["vector_store"]["config"]["path"]

    def build_memory() -> AsyncMemory:
        # Mem0 and its Chroma store install on first use, and Mem0's huggingface embedder, which both local providers
        # load, imports sentence-transformers. Building the memory imports Chroma and the provider SDKs, so it waits
        # until no install runs. All of it runs in a worker thread, so the event loop keeps serving other tasks.
        ensure_optional_deps(["mem0ai"], "mem0", runtime_paths)
        ensure_optional_deps(["chromadb"], "chromadb", runtime_paths)
        if config_dict["embedder"]["provider"] == "huggingface":
            ensure_sentence_transformers_dependencies(runtime_paths)
        with _STORE_BUILD_LOCKS.setdefault(store_path, threading.Lock()), optional_dependency_environment():
            from mem0 import AsyncMemory  # noqa: PLC0415

            try:
                return AsyncMemory.from_config(config_dict)
            except Exception as exc:
                # Mem0's Ollama embedder asks the server for its model while the memory is built, the only request
                # building it sends, so an Ollama failure here is the embedder's.
                if config_dict["embedder"]["provider"] != "ollama" or (failure := _ollama_failure(exc)) is None:
                    raise
                raise embedder_request_error(exc, failure) from exc

    memory = await asyncio.to_thread(build_memory)
    memory_llm = config.memory.llm
    llm_config = memory.llm.config
    if (
        memory_llm is not None
        and memory_llm.provider in {"openai", "openrouter"}
        and llm_config.is_reasoning_model is None
        and openai_rejects_sampling_controls(str(llm_config.model))
    ):
        # Mem0 knows only a few GPT-5 IDs as reasoning models and sends every other model temperature, top_p, and a
        # token cap. GPT-5 and GPT-6 models, directly or through OpenRouter, reject max_tokens and most of them those
        # sampling values, and their max_completion_tokens cap would also count reasoning, so they get Mem0's
        # reasoning-model request instead, unless the config sets ``is_reasoning_model`` itself.
        llm_config.is_reasoning_model = True
    install_mem0_usage_capture(memory.llm, memory_llm.provider if memory_llm is not None else "ollama")
    if config.memory.embedder.provider == "openai":
        # Mem0's own OpenAI embedder indexes response.data[0] without validation
        # and can expose raw provider errors. Reuse MindRoom's strict boundary.
        from mindroom.embeddings.factory import create_configured_embedder  # noqa: PLC0415

        strict_embedder = create_configured_embedder(config, runtime_paths)
        cast("Any", memory).embedding_model = _Mem0StrictOpenAIEmbedder(strict_embedder)
    elif config.memory.embedder.provider == "ollama":
        cast("Any", memory).embedding_model = _Mem0ClassifiedOllamaEmbedder(memory.embedding_model)

    logger.info("created_memory_instance", path=store_path)
    return memory
