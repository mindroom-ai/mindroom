"""Removal of the Chroma files knowledge bases kept before MindRoom owned its vector store."""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from pathlib import Path

from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root

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
    """Remove the Chroma database and segment directories one base's index directory still holds.

    The directory is reached by a no-follow walk from the filesystem root, so a directory a worker swapped for a link,
    such as one pointing at a Mem0 Chroma store, is refused rather than emptied.
    """
    removed = 0
    try:
        with open_directory_within_root(Path(storage.anchor), storage.relative_to(storage.anchor)) as directory:
            for name in os.listdir(directory):  # noqa: PTH208 - lists the pinned descriptor, not a path
                try:
                    if name in _CHROMA_FILES:
                        os.unlink(name, dir_fd=directory)
                    elif str(uuid.UUID(name)) == name and stat.S_ISDIR(
                        os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode,
                    ):
                        shutil.rmtree(name, dir_fd=directory)
                    else:
                        continue
                except (OSError, ValueError):
                    continue
                removed += 1
    except OSError:
        logger.warning("Skipped legacy Chroma knowledge storage cleanup", base_id=base_id, exc_info=True)
        return
    if removed:
        logger.info("Removed legacy Chroma knowledge storage", base_id=base_id, entries=removed)
