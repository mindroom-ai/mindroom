"""Embedding helpers for OpenAI-compatible and local providers."""

from __future__ import annotations

import importlib
import threading
from typing import TYPE_CHECKING, Any, cast

from mindroom.model_defaults import OPENAI_EMBEDDING_DIMENSIONS, SENTENCE_TRANSFORMERS_DEFAULT
from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.knowledge.embedder.base import Embedder

    from mindroom.constants import RuntimePaths

# EmbeddingGemma 2 loads its video processor, which needs torchvision, even for text.
_SENTENCE_TRANSFORMERS_DEPENDENCIES = ["sentence-transformers", "torchvision"]
_SENTENCE_TRANSFORMERS_EXTRA = "sentence_transformers"
_SENTENCE_TRANSFORMER_CLIENT: tuple[str, Any] | None = None

#: One torch model per process is shared by every indexing thread, and torch's
#: Metal shader-library caches (``libMap``, ``cplMap``, ``kernelCache`` in
#: ``at::native::mps::MetalShaderLibrary``) are plain ``std::unordered_map``
#: instances with no lock. Torch drops the GIL inside those calls, so two
#: threads embedding at once can corrupt a bucket chain: the process then
#: either spins forever in a cache lookup or dies with SIGSEGV. Local
#: embedding runs one call at a time. Nothing is lost by serializing, because
#: the work already funnels through a single GPU queue.
_LOCAL_EMBEDDING_LOCK = threading.RLock()


def _default_dimensions(model: str) -> int | None:
    """Return the default dimensions for models that support the parameter."""
    return OPENAI_EMBEDDING_DIMENSIONS.get(model)


def effective_knowledge_embedder_signature(
    provider: str,
    model: str,
    *,
    host: str | None = None,
    dimensions: int | None = None,
) -> tuple[str, str, str, str]:
    """Return the settings that decide whether vectors from MindRoom's own embedder are compatible.

    Knowledge, file-memory, and knowledge-graph vectors come from MindRoom's embedder, which embeds
    sentence-transformers queries and documents with the model's own prompts. The marked model keeps
    vectors built before those prompts were applied from being queried or reused. Mem0 embeds through
    its own client without the prompts, so its signature keeps the plain model.
    """
    if provider == "sentence_transformers":
        model = f"{model}#query-document-prompts"
    return effective_mem0_embedder_signature(provider, model, host=host, dimensions=dimensions)


def effective_mem0_embedder_signature(
    provider: str,
    model: str,
    *,
    host: str | None = None,
    dimensions: int | None = None,
) -> tuple[str, str, str, str]:
    """Return the Mem0 embedder settings that affect memory collection compatibility."""
    effective_host = host if provider in {"openai", "ollama"} else ""
    effective_dimensions = dimensions
    if provider == "openai" and effective_dimensions is None:
        effective_dimensions = _default_dimensions(model)
    elif provider in {"ollama", "sentence_transformers"}:
        effective_dimensions = None
    return (
        provider,
        model,
        effective_host or "",
        str(effective_dimensions) if effective_dimensions is not None else "",
    )


def ensure_sentence_transformers_dependencies(runtime_paths: RuntimePaths) -> None:
    """Install the optional local sentence-transformers runtime when needed."""
    ensure_optional_deps(_SENTENCE_TRANSFORMERS_DEPENDENCIES, _SENTENCE_TRANSFORMERS_EXTRA, runtime_paths)


def _serialized_sentence_transformers_class() -> Callable[..., Embedder]:
    """Return the local embedder class with its torch calls serialized."""
    # Imported with the embedder module so Agno stays out of this module's import time.
    from agno.knowledge.embedder.base import raise_embedding_error  # noqa: PLC0415

    module = importlib.import_module("agno.knowledge.embedder.sentence_transformer")
    base_class = cast("Any", module.SentenceTransformerEmbedder)

    class SerializedSentenceTransformerEmbedder(base_class):  # type: ignore[misc, valid-type]
        """Local embedder that admits one thread at a time into torch and applies the model's prompts."""

        # AGNO_COMPAT: Embedders cannot tell a search query from a document.
        # Reason: Agno's Embedder has one embedding entry point for both, so its SentenceTransformer embedder
        # applies one fixed prompt to queries and documents alike. Models such as EmbeddingGemma 2 define
        # separate query and document prompts and retrieve worse without them. MindRoom infers the purpose
        # from the method: Agno's vector stores and MindRoom's read path embed queries with ``get_embedding``,
        # while ``Document.embed`` and MindRoom's prefetch embed chunks with ``get_embedding_and_usage``.
        # Upstream issue: tracking gap; searching agno-agi/agno issues and PRs found no query/document input type.
        # Upstream PR: none identified.
        # Remove when: Agno's Embedder exposes separate query and document embedding and its vector stores call
        # them; keep applying the model's own query and document prompts.
        # Coverage: tests/test_embeddings.py::test_sentence_transformers_embedder_embeds_queries_and_documents_with_their_prompts,
        # tests/test_knowledge_resumable_refresh.py::test_batch_prefetch_embeds_chunks_as_documents.
        def _encode(self, text: str, *, query: bool) -> list[float]:
            client = self.sentence_transformer_client
            encode = client.encode_query if query else client.encode_document
            with _LOCAL_EMBEDDING_LOCK:
                try:
                    embedding = encode(text, normalize_embeddings=self.normalize_embeddings)
                except Exception as exc:
                    raise_embedding_error(exc, model_id=self.id, provider="SentenceTransformer")
            return cast("list[float]", embedding.tolist())

        def get_embedding(self, text: str) -> list[float]:
            return self._encode(text, query=True)

        def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
            return self._encode(text, query=False), None

    return cast("Callable[..., Embedder]", SerializedSentenceTransformerEmbedder)


def create_sentence_transformers_embedder(
    runtime_paths: RuntimePaths,
    model: str = SENTENCE_TRANSFORMERS_DEFAULT,
    *,
    dimensions: int | None = None,
) -> Embedder:
    """Create a local sentence-transformers embedder after ensuring its optional extra exists."""
    ensure_sentence_transformers_dependencies(runtime_paths)
    with optional_dependency_environment(), _LOCAL_EMBEDDING_LOCK:
        embedder_class = _serialized_sentence_transformers_class()
        kwargs: dict[str, Any] = {"id": model}
        if dimensions is not None:
            kwargs["dimensions"] = dimensions

        global _SENTENCE_TRANSFORMER_CLIENT
        if _SENTENCE_TRANSFORMER_CLIENT is None or _SENTENCE_TRANSFORMER_CLIENT[0] != model:
            embedder = cast("Any", embedder_class(**kwargs))
            _SENTENCE_TRANSFORMER_CLIENT = (model, embedder.sentence_transformer_client)
            return cast("Embedder", embedder)
        client = _SENTENCE_TRANSFORMER_CLIENT[1]
        return embedder_class(**kwargs, sentence_transformer_client=client)
