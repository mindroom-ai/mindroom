"""Memory configuration and setup."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from mindroom.credentials.environment import get_secret_from_env
from mindroom.credentials.sync import get_api_key_for_provider, get_ollama_host, get_openai_base_url
from mindroom.embeddings.errors import EmbedderRequestError, embedder_request_error, openai_embedder_error
from mindroom.embeddings.factory import resolve_embedder_settings
from mindroom.embeddings.signatures import effective_mem0_embedder_signature, ensure_sentence_transformers_dependencies
from mindroom.logging_config import get_logger
from mindroom.memory._mem0_usage import install_mem0_usage_capture
from mindroom.model_defaults import (
    ANTHROPIC_BASE_URL_DEFAULT,
    MEMORY_OLLAMA_LLM,
    OPENROUTER_BASE_URL_DEFAULT,
    claude_rejects_sampling_controls,
    openai_rejects_sampling_controls,
)
from mindroom.timing import timed
from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mem0 import AsyncMemory

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.embeddings.base import Embedder, EmbeddingPurpose
    from mindroom.llm.errors import ProviderError

_T = TypeVar("_T")

logger = get_logger(__name__)
MEM0_COLLECTION_PREFIX = "mindroom_memories"
_CHROMA_DIRNAME = "chroma"
# The calls into one Chroma store take turns, from building a memory on. Chroma publishes each store's shared system
# before starting it, so a build that overlaps the first build of the same store can reach the unstarted system, and
# both builds fail. A query that overlaps a new collection's first write can fail with "Error creating hnsw segment
# reader: Nothing found on disk", as a first memory search beside the first save into a store did. Chroma keys that
# shared state by store path, so different stores still work in parallel.
_STORE_LOCKS: dict[str, threading.Lock] = {}


@dataclass(frozen=True)
class _Mem0StrictOpenAIEmbedder:
    """Adapt MindRoom's strict OpenAI embedder, which raises typed embedder errors, to Mem0's embedding interface."""

    embedder: Embedder

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
        return self.embedder.embed([text.replace("\n", " ") for text in texts], purpose=purpose)


def _ollama_failure(error: Exception) -> ProviderError | None:
    """Return the typed provider error of an Ollama SDK or transport failure, as MindRoom's Ollama embedder maps it."""
    from mindroom.providers.ollama.errors import provider_error  # noqa: PLC0415 - loads the Ollama SDK

    return provider_error(error, provider="ollama")


def _ollama_embedder_error(error: Exception) -> EmbedderRequestError:
    """Return the typed embedder error of a failure of Mem0's own Ollama embedder."""
    return embedder_request_error(error, _ollama_failure(error))


# The embedder failures of the Mem0 operation in progress, including those Mem0 catches: it drops the extracted facts
# it could not embed and reports the add as done. Mem0 embeds in worker threads, which inherit this context.
_operation_failures: ContextVar[list[EmbedderRequestError] | None] = ContextVar("mem0_embedder_failures", default=None)


@contextmanager
def recorded_embedder_failures() -> Iterator[list[EmbedderRequestError]]:
    """Collect every embedder failure of the Mem0 operation run inside, in order."""
    failures: list[EmbedderRequestError] = []
    token = _operation_failures.set(failures)
    try:
        yield failures
    finally:
        _operation_failures.reset(token)


def _recorded(error: EmbedderRequestError) -> EmbedderRequestError:
    if (failures := _operation_failures.get()) is not None:
        failures.append(error)
    return error


@dataclass(frozen=True)
class _Mem0Embedder:
    """The embedder every Mem0 memory calls, which raises typed embedder errors and records each failure.

    ``embedder`` is Mem0's own embedder, whose requests go out unchanged so stored memories keep matching their
    searches, or MindRoom's strict OpenAI embedder; ``classify`` returns the typed error of one failure of its SDK.
    """

    embedder: Any
    classify: Callable[[Exception], EmbedderRequestError]

    def embed(self, text: str, memory_action: Literal["add", "search", "update"] | None = None) -> list[float]:
        return self._send(self.embedder.embed, text, memory_action)

    def embed_batch(
        self,
        texts: list[str],
        memory_action: Literal["add", "search", "update"] | None = "add",
    ) -> list[list[float]]:
        return self._send(self.embedder.embed_batch, texts, memory_action)

    def _send(self, call: Callable[..., _T], *args: object) -> _T:
        try:
            return call(*args)
        except EmbedderRequestError as error:
            _recorded(error)
            raise
        except Exception as exc:
            raise _recorded(self.classify(exc)) from exc


def _hold_store_lock(vector_store: Any, lock: threading.Lock) -> None:  # noqa: ANN401 - Mem0's Chroma vector store
    """Make each call Mem0 sends its vector store, from a worker thread, hold the store's lock.

    The entity store Mem0 builds on first use keeps unlocked calls; Mem0 catches their failures.
    """

    def locked(call: Callable[..., _T]) -> Callable[..., _T]:
        def locked_call(*args: object, **kwargs: object) -> _T:
            with lock:
                return call(*args, **kwargs)

        return locked_call

    vector_store.search = locked(vector_store.search)
    vector_store.insert = locked(vector_store.insert)
    vector_store.update = locked(vector_store.update)
    vector_store.delete = locked(vector_store.delete)
    vector_store.get = locked(vector_store.get)
    vector_store.list = locked(vector_store.list)


def mem0_collection_name(config: Config) -> str:
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
    return f"{MEM0_COLLECTION_PREFIX}_{digest}"


def mem0_chroma_path(store_root: Path) -> Path:
    """Return the Chroma directory of the Mem0 store under ``store_root``."""
    return store_root / _CHROMA_DIRNAME


def mem0_store_exists(store_root: Path) -> bool:
    """Return whether a Mem0 store was ever built under ``store_root``, without building one."""
    return mem0_chroma_path(store_root).is_dir()


def mem0_ollama_host(config: Config, runtime_paths: RuntimePaths) -> str:
    """Return the server the Ollama memory LLM, configured or default, reaches.

    Mem0's own ``ollama_base_url`` goes as written; otherwise the saved Ollama host wins over ``memory.llm.config.host``.
    """
    llm_config = {} if config.memory.llm is None else config.memory.llm.config
    if base_url := llm_config.get("ollama_base_url"):
        return base_url
    return get_ollama_host(runtime_paths=runtime_paths, configured_host=llm_config.get("host"), saved_first=True)


def _mem0_llm_config(config: Config, runtime_paths: RuntimePaths) -> dict[str, Any]:
    """Build the Mem0 LLM config from ``memory.llm``, defaulting to local Ollama."""
    memory_llm = config.memory.llm
    if memory_llm is None:
        logger.warning(f"No memory LLM configured, using default ollama/{MEMORY_OLLAMA_LLM}")
        return {
            "provider": "ollama",
            "config": {
                "model": MEMORY_OLLAMA_LLM,
                "ollama_base_url": mem0_ollama_host(config, runtime_paths),
                "temperature": 0.1,
                "top_p": 1,
            },
        }

    llm_provider = memory_llm.provider
    # Mem0 names the Ollama server ``ollama_base_url``, and its other LLMs read no ``host``.
    llm_provider_config = {key: value for key, value in memory_llm.config.items() if key != "host"}
    if llm_provider == "ollama":
        llm_provider_config["ollama_base_url"] = mem0_ollama_host(config, runtime_paths)
    elif llm_provider == "openai" and not llm_provider_config.get("openai_base_url"):
        llm_provider_config["openai_base_url"] = get_openai_base_url(runtime_paths)
    elif llm_provider == "anthropic" and not llm_provider_config.get("anthropic_base_url"):
        # Mem0's client would read ANTHROPIC_BASE_URL from the process env alone.
        llm_provider_config["anthropic_base_url"] = (
            get_secret_from_env("ANTHROPIC_BASE_URL", runtime_paths=runtime_paths) or ANTHROPIC_BASE_URL_DEFAULT
        )

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


def _get_memory_config(store_root: Path, config: Config, runtime_paths: RuntimePaths) -> dict:
    """Get Mem0 configuration with a ChromaDB backend and history database under ``store_root``.

    Args:
        store_root: The root of one Mem0 store: an agent's state root or the team memory root
        config: Application configuration
        runtime_paths: Explicit runtime context for credential-backed provider settings.

    Returns:
        Configuration dictionary for Mem0

    """
    # Canonicalize once so Chroma path is independent of runtime cwd changes.
    resolved_store_root = store_root.expanduser().resolve()

    # Ensure storage directories exist
    chroma_path = mem0_chroma_path(resolved_store_root)
    chroma_path.mkdir(parents=True, exist_ok=True)
    # Mem0's history database keeps the text of changed and deleted memories, and the latest messages it adds to the
    # next extraction prompt under keys that do not name the requester, so each store root needs its own.
    history_dir = resolved_store_root / "mem0"
    history_dir.mkdir(exist_ok=True)
    resolved_embedder = resolve_embedder_settings(config, runtime_paths)
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
        embedder_provider_config["openai_base_url"] = resolved_embedder.host
        if resolved_embedder.dimensions is not None:
            embedder_provider_config["embedding_dims"] = resolved_embedder.dimensions
    elif embedder_provider == "ollama":
        embedder_provider_config["ollama_base_url"] = resolved_embedder.host

    return {
        "embedder": embedder_config,
        "llm": _mem0_llm_config(config, runtime_paths),
        "vector_store": {
            "provider": "chroma",
            "config": {
                "collection_name": mem0_collection_name(config),
                "path": str(chroma_path),
            },
        },
        "history_db_path": str(history_dir / "history.db"),
    }


@timed("system_prompt_assembly.memory_search.mem0.async_memory_from_config")
async def create_memory_instance(
    store_root: Path,
    config: Config,
    runtime_paths: RuntimePaths,
) -> AsyncMemory:
    """Create a Mem0 memory instance with ChromaDB backend.

    Args:
        store_root: The root of one Mem0 store: an agent's state root or the team memory root
        config: Application configuration
        runtime_paths: Explicit runtime context for credential-backed provider settings.

    Returns:
        Configured AsyncMemory instance

    """
    config_dict = _get_memory_config(store_root, config, runtime_paths)
    store_path = config_dict["vector_store"]["config"]["path"]
    store_lock = _STORE_LOCKS.setdefault(store_path, threading.Lock())

    def build_memory() -> AsyncMemory:
        # Mem0 and its Chroma store install on first use, and Mem0's huggingface embedder, which both local providers
        # load, imports sentence-transformers. Building the memory imports Chroma and the provider SDKs, so it waits
        # until no install runs. All of it runs in a worker thread, so the event loop keeps serving other tasks.
        ensure_optional_deps(["mem0ai"], "mem0", runtime_paths)
        ensure_optional_deps(["chromadb"], "chromadb", runtime_paths)
        if config_dict["embedder"]["provider"] == "huggingface":
            ensure_sentence_transformers_dependencies(runtime_paths)
        with store_lock, optional_dependency_environment():
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
    embedder_provider = config.memory.embedder.provider
    if embedder_provider == "openai":
        # Mem0's own OpenAI embedder indexes response.data[0] without validation
        # and can expose raw provider errors. Reuse MindRoom's strict boundary.
        from mindroom.embeddings.factory import create_configured_embedder  # noqa: PLC0415

        mem0_embedder = _Mem0StrictOpenAIEmbedder(create_configured_embedder(config, runtime_paths))
    else:
        mem0_embedder = memory.embedding_model
    # Mem0's Ollama embedder fails through the Ollama SDK, and the embedders only Mem0 provides mostly use the OpenAI SDK;
    # MindRoom's strict embedder raises typed errors itself.
    classify = _ollama_embedder_error if embedder_provider == "ollama" else openai_embedder_error
    cast("Any", memory).embedding_model = _Mem0Embedder(mem0_embedder, classify)
    _hold_store_lock(memory.vector_store, store_lock)

    logger.info("created_memory_instance", path=store_path)
    return memory
