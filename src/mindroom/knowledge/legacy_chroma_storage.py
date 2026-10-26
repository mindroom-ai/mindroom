"""Removal of the Chroma files knowledge bases kept before MindRoom owned its vector store."""

from __future__ import annotations

import shutil
import uuid
from typing import TYPE_CHECKING, Any, cast

from mindroom.knowledge.registry import (
    load_published_index_state,
    published_index_metadata_path,
    resolve_published_index_key,
)
from mindroom.knowledge.vector_store import StoredRow
from mindroom.logging_config import get_logger
from mindroom.tool_system.dependencies import ensure_optional_deps, optional_dependency_environment

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.indexing_config import IndexingSettings
    from mindroom.knowledge.vector_store import KnowledgeCollection

logger = get_logger(__name__)


# LEGACY_COMPAT: Knowledge publications made by the Chroma-backed store.
# Legacy format: Published indexing settings without vector_store; the base's vectors are in its Chroma database.
# Last legacy release: v2026.10.568; replacement: v2026.10.569 stores semantic indexes in owned SQLite files.
# Handling: Such a publication stays reusable, its rows copied out of Chroma, and at startup every configured shared base
# that still has one gets that copy scheduled, smallest first, so conversion starts at boot instead of on first use.
# Coverage: tests/test_knowledge_collection_cleanup.py::test_a_chroma_era_index_is_copied_without_embedding_again,
# tests/test_knowledge_manager.py::test_source_watcher_sync_converts_chroma_era_bases_once.
def chroma_era_publication(settings: IndexingSettings) -> bool:
    """Return whether published settings come from the Chroma-backed store."""
    return settings.mode == "semantic" and not settings.vector_store


def chroma_era_shared_base_ids(config: Config, runtime_paths: RuntimePaths) -> list[str]:
    """Return the configured shared semantic bases still published from Chroma, smallest first."""
    found: list[tuple[int, str]] = []
    for base_id in config.knowledge_bases:
        if config.get_knowledge_base_config(base_id).mode != "semantic":
            continue
        if config.get_private_knowledge_base_agent(base_id) is not None:
            continue
        try:
            key = resolve_published_index_key(base_id, config=config, runtime_paths=runtime_paths)
        except (OSError, ValueError):
            continue
        state = load_published_index_state(published_index_metadata_path(key))
        if state is not None and chroma_era_publication(state.settings):
            found.append((state.indexed_count or 0, base_id))
    return [base_id for _count, base_id in sorted(found)]


# LEGACY_COMPAT: Chroma-era knowledge index files.
# Legacy format: A base's index directory held chroma.sqlite3 (with its journal, WAL, and shared-memory files) and
# UUID-named HNSW segment directories.
# Last legacy release: v2026.10.568; replacement: v2026.10.569 stores semantic indexes in owned SQLite files.
# Handling: The first owned candidate copies the published Chroma collection's rows verbatim, so upgrading re-embeds
# nothing; the sweep after that candidate is published removes the Chroma files.
# Coverage: tests/test_knowledge_collection_cleanup.py::test_a_chroma_era_index_is_copied_without_embedding_again.
_CHROMA_FILE = "chroma.sqlite3"
_CHROMA_FILES = frozenset({_CHROMA_FILE, f"{_CHROMA_FILE}-journal", f"{_CHROMA_FILE}-wal", f"{_CHROMA_FILE}-shm"})
#: Chroma rows read per page while copying a Chroma-era collection.
_COPY_PAGE_ROWS = 500


def legacy_chroma_store_exists(storage: Path) -> bool:
    """Return whether one base's index directory still holds a Chroma database."""
    return (storage / _CHROMA_FILE).is_file()


def copy_legacy_chroma_rows(
    storage: Path,
    collection_name: str,
    candidate: KnowledgeCollection,
    *,
    select: Callable[[Mapping[str, Any]], bool],
    runtime_paths: RuntimePaths,
) -> None:
    """Copy the rows of one Chroma-era collection that ``select`` accepts into an owned candidate, verbatim.

    Needs the ``chromadb`` extra, installed on first use like the Mem0 backend's; when it cannot be installed the
    caller re-embeds instead.
    """
    ensure_optional_deps(["chromadb"], "chromadb", runtime_paths)
    with optional_dependency_environment():
        import chromadb  # noqa: PLC0415

    client = chromadb.PersistentClient(path=str(storage), settings=chromadb.Settings(anonymized_telemetry=False))
    try:
        collection = client.get_collection(collection_name)
        offset = 0
        while True:
            page = collection.get(
                limit=_COPY_PAGE_ROWS,
                offset=offset,
                include=["embeddings", "documents", "metadatas"],
            )
            if not page["ids"]:
                return
            offset += len(page["ids"])
            # Chroma types every field optional; this page asked for all three.
            documents = cast("list[str | None]", page["documents"])
            metadatas = cast("list[Mapping[str, Any]]", page["metadatas"])
            embeddings = cast("list[Any]", page["embeddings"])
            candidate.add_rows(
                [
                    StoredRow(
                        id=row_id,
                        document=document or "",
                        metadata=metadata,
                        embedding=embedding.astype("<f4").tobytes(),
                    )
                    for row_id, document, metadata, embedding in zip(
                        page["ids"],
                        documents,
                        metadatas,
                        embeddings,
                        strict=True,
                    )
                    if select(metadata)
                ],
            )
    finally:
        client.close()


def remove_legacy_chroma_storage(storage: Path, *, base_id: str) -> None:
    """Remove the Chroma database and segment directories one base's index directory still holds."""
    removed = 0
    try:
        entries = tuple(storage.iterdir())
    except OSError:
        return
    for path in entries:
        try:
            if path.name in _CHROMA_FILES:
                path.unlink()
            elif not path.is_symlink() and path.is_dir() and str(uuid.UUID(path.name)) == path.name:
                shutil.rmtree(path)
            else:
                continue
        except (OSError, ValueError):
            continue
        removed += 1
    if removed:
        logger.info("Removed legacy Chroma knowledge storage", base_id=base_id, entries=removed)
