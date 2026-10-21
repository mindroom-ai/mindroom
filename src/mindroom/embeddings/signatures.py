"""Compatibility signatures for semantic embedders, and the local embedder's optional dependency install."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from mindroom.model_defaults import OPENAI_EMBEDDING_DIMENSIONS
from mindroom.tool_system.dependencies import ensure_optional_deps

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

# EmbeddingGemma 2 loads its video processor, which needs torchvision, even for text.
_SENTENCE_TRANSFORMERS_DEPENDENCIES = ["sentence-transformers", "torchvision"]
_SENTENCE_TRANSFORMERS_EXTRA = "sentence_transformers"


def _default_dimensions(model: str) -> int | None:
    """Return the default dimensions for models that support the parameter."""
    return OPENAI_EMBEDDING_DIMENSIONS.get(model)


def effective_knowledge_embedder_signature(
    provider: str,
    model: str,
    *,
    host: str | None = None,
    dimensions: int | None = None,
    query_prefix: str | None = None,
    document_prefix: str | None = None,
) -> tuple[str, str, str, str]:
    """Return the settings that decide whether vectors from MindRoom's own embedder are compatible.

    Knowledge, file-memory, and knowledge-graph vectors come from MindRoom's embedder, which embeds
    sentence-transformers queries and documents with the model's own prompts. The marked model keeps
    vectors built before those prompts were applied from being queried or reused. Mem0 embeds through
    its own client without the prompts, so its signature keeps the plain model.
    """
    if provider == "sentence_transformers":
        model = f"{model}#query-document-prompts"
    return effective_mem0_embedder_signature(
        provider,
        model,
        host=host,
        dimensions=dimensions,
        query_prefix=query_prefix,
        document_prefix=document_prefix,
    )


def effective_mem0_embedder_signature(
    provider: str,
    model: str,
    *,
    host: str | None = None,
    dimensions: int | None = None,
    query_prefix: str | None = None,
    document_prefix: str | None = None,
) -> tuple[str, str, str, str]:
    """Return the Mem0 embedder settings that affect memory collection compatibility.

    Prefixes change every vector the OpenAI-compatible client returns, so they mark the model; without them the
    signature is unchanged and existing collections keep their names.
    """
    effective_host = host if provider in {"openai", "ollama"} else ""
    effective_dimensions = dimensions
    if provider == "openai" and effective_dimensions is None:
        effective_dimensions = _default_dimensions(model)
    elif provider in {"ollama", "sentence_transformers"}:
        effective_dimensions = None
    if provider == "openai" and (query_prefix or document_prefix):
        model = f"{model}#prefixes={json.dumps([query_prefix or '', document_prefix or ''])}"
    return (
        provider,
        model,
        effective_host or "",
        str(effective_dimensions) if effective_dimensions is not None else "",
    )


def ensure_sentence_transformers_dependencies(runtime_paths: RuntimePaths) -> None:
    """Install the optional local sentence-transformers runtime when needed."""
    ensure_optional_deps(_SENTENCE_TRANSFORMERS_DEPENDENCIES, _SENTENCE_TRANSFORMERS_EXTRA, runtime_paths)
