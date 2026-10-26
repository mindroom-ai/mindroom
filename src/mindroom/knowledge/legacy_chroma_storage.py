"""Removal of the Chroma files knowledge bases kept before MindRoom owned its vector store."""

from __future__ import annotations

import shutil
import uuid
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

# LEGACY_COMPAT: Chroma-era knowledge index files.
# Legacy format: A base's index directory held chroma.sqlite3 (with its journal, WAL, and shared-memory files) and
# UUID-named HNSW segment directories.
# Last legacy release: v2026.10.552; replacement: v2026.10.553 stored semantic indexes in owned SQLite files.
# Handling: The sweep after a base's first owned publication removes them; nothing reads them anymore.
# Coverage: tests/test_knowledge_collection_cleanup.py::test_a_chroma_era_index_rebuilds_and_its_files_go_only_after_publication.
_CHROMA_FILE = "chroma.sqlite3"
_CHROMA_FILES = frozenset({_CHROMA_FILE, f"{_CHROMA_FILE}-journal", f"{_CHROMA_FILE}-wal", f"{_CHROMA_FILE}-shm"})


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
