"""Semantic search for file-backed memory roots."""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import TYPE_CHECKING

from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.knowledge import KnowledgeRefreshScheduler, list_knowledge_files, resolve_knowledge_base_access
from mindroom.logging_config import get_logger
from mindroom.memory._shared import MemoryResult
from mindroom.timing import emit_elapsed_timing

if TYPE_CHECKING:
    from pathlib import Path

    from agno.knowledge.document.base import Document

    from mindroom.config.main import Config
    from mindroom.config.memory import MemorySearchConfig
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_SOURCE_PATH_KEY = "source_path"
_CHUNK_SIZE = 5000
_CHUNK_OVERLAP = 0
_MEMORY_KNOWLEDGE_PREFIX = "file_memory"
_memory_refresh_scheduler = KnowledgeRefreshScheduler()


class SemanticFileMemoryIndexUnavailableError(RuntimeError):
    """Raised when semantic file memory should use keyword fallback for this request."""


def _safe_identifier(value: str) -> str:
    sanitized = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return sanitized or "default"


def _scope_digest(root: Path, scope_user_id: str) -> str:
    return hashlib.sha256(f"{scope_user_id}:{root.resolve()}".encode()).hexdigest()[:16]


def _memory_knowledge_base_id(root: Path, scope_user_id: str) -> str:
    return f"{_MEMORY_KNOWLEDGE_PREFIX}_{_safe_identifier(scope_user_id)}_{_scope_digest(root, scope_user_id)}"


def _memory_include_patterns(search_config: MemorySearchConfig) -> list[str]:
    patterns = list(search_config.include)
    if search_config.include_entrypoint:
        patterns.append("MEMORY.md")
    return patterns


def _memory_knowledge_config(
    config: Config,
    *,
    base_id: str,
    root: Path,
    search_config: MemorySearchConfig,
) -> Config:
    knowledge_config = config.model_copy(deep=True)
    knowledge_config.knowledge_bases[base_id] = KnowledgeBaseConfig(
        mode="semantic",
        description="File-backed memory search index",
        path=str(root.resolve()),
        watch=False,
        chunk_size=_CHUNK_SIZE,
        chunk_overlap=_CHUNK_OVERLAP,
        include_extensions=[".md"],
        include_patterns=_memory_include_patterns(search_config),
    )
    return knowledge_config


async def _list_memory_knowledge_files(config: Config, base_id: str, root: Path) -> list[Path]:
    return await asyncio.to_thread(list_knowledge_files, config, base_id, root)


def _memory_results_from_documents(
    documents: list[Document],
    *,
    scope_user_id: str,
) -> list[MemoryResult]:
    results: list[MemoryResult] = []
    for rank, document in enumerate(documents, start=1):
        metadata = dict(document.meta_data)
        source_file = metadata.get(_SOURCE_PATH_KEY)
        if not isinstance(source_file, str):
            source_file = "memory"
        content = " ".join(document.content.split())
        if not content:
            continue
        score = document.reranking_score
        results.append(
            MemoryResult(
                id=f"semantic:{source_file}:{rank}",
                memory=content,
                user_id=scope_user_id,
                score=float(score) if score is not None else 1.0 - (rank * 0.000001),
                metadata={"source_file": source_file, "semantic": True, "search_mode": "semantic"},
            ),
        )
    return results


async def search_semantic_file_memories(
    query: str,
    *,
    scope_user_id: str,
    root: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    search_config: MemorySearchConfig,
    limit: int,
    timing_scope: str | None = None,
) -> list[MemoryResult]:
    """Search one file-memory scope through the published knowledge index pipeline."""
    base_id = _memory_knowledge_base_id(root, scope_user_id)
    knowledge_config = _memory_knowledge_config(
        config,
        base_id=base_id,
        root=root,
        search_config=search_config,
    )

    list_start = time.monotonic()
    files = await _list_memory_knowledge_files(knowledge_config, base_id, root)
    emit_elapsed_timing(
        "system_prompt_assembly.memory_search.semantic.file_listing",
        list_start,
        timing_scope=timing_scope,
        file_count=len(files),
        include_pattern_count=len(search_config.include),
        include_entrypoint=search_config.include_entrypoint,
    )
    if not files:
        return []

    access_start = time.monotonic()
    resolution = resolve_knowledge_base_access(base_id, knowledge_config, runtime_paths)
    _memory_refresh_scheduler.schedule_refresh(base_id, config=knowledge_config, runtime_paths=runtime_paths)
    emit_elapsed_timing(
        "system_prompt_assembly.memory_search.semantic.published_index_access",
        access_start,
        timing_scope=timing_scope,
        availability=resolution.availability.value,
        refresh_scheduled=True,
    )
    if resolution.knowledge is None:
        msg = "Semantic file-memory index is not ready"
        raise SemanticFileMemoryIndexUnavailableError(msg)

    query_start = time.monotonic()
    documents: list[Document] = await asyncio.to_thread(resolution.knowledge.search, query=query, max_results=limit)
    emit_elapsed_timing(
        "system_prompt_assembly.memory_search.semantic.vector_query",
        query_start,
        timing_scope=timing_scope,
        availability=resolution.availability.value,
    )
    return _memory_results_from_documents(documents, scope_user_id=scope_user_id)
