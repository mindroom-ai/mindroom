"""Embedding helpers for OpenAI-compatible and local providers."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any, cast

from mindroom.model_defaults import OPENAI_EMBEDDING_DIMENSIONS, SENTENCE_TRANSFORMERS_DEFAULT
from mindroom.tool_system.dependencies import ensure_optional_deps

if TYPE_CHECKING:
    from agno.knowledge.embedder.base import Embedder

    from mindroom.constants import RuntimePaths

_SENTENCE_TRANSFORMERS_DEPENDENCIES = ["sentence-transformers"]
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
) -> tuple[str, str, str, str]:
    """Return the knowledge embedder settings that affect indexing behavior."""
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


def create_sentence_transformers_embedder(
    runtime_paths: RuntimePaths,
    model: str = SENTENCE_TRANSFORMERS_DEFAULT,
    *,
    dimensions: int | None = None,
) -> Embedder:
    """Create a local sentence-transformers embedder after ensuring its optional extra exists."""
    ensure_sentence_transformers_dependencies(runtime_paths)
    module = importlib.import_module("agno.knowledge.embedder.sentence_transformer")
    embedder_class = cast("Any", module.SentenceTransformerEmbedder)
    if dimensions is None:
        return cast("Embedder", embedder_class(id=model))
    return cast("Embedder", embedder_class(id=model, dimensions=dimensions))
