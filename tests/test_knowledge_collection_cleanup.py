"""Integration tests for physical knowledge collection cleanup."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from mindroom.knowledge.collections import (
    CollectionSpace,
    cleanup_superseded_collections,
    delete_collection,
    open_collection,
)
from mindroom.knowledge.vector_store import list_collection_names
from tests.knowledge_test_support import seed_collection, stored_rows

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_cleanup_removes_legacy_chroma_storage_and_leaves_other_files(tmp_path: Path) -> None:
    """Deletion and cleanup reclaim superseded collections and old Chroma files while preserving everything else."""
    storage_path = tmp_path / "storage"
    space = CollectionSpace(
        base_id="docs",
        knowledge_path=tmp_path / "docs",
        storage_path=storage_path,
    )
    live = open_collection(space, space.default_collection)
    seed_collection(live, [{"content": "live"}])
    deleted = open_collection(space, f"{space.default_collection}_candidate_deleted")
    seed_collection(deleted, [{"content": "deleted"}])
    stale = open_collection(space, f"{space.default_collection}_candidate_stale")
    seed_collection(stale, [{"content": "stale"}])

    legacy_files = [storage_path / name for name in ("chroma.sqlite3", "chroma.sqlite3-wal", "chroma.sqlite3-shm")]
    for legacy_file in legacy_files:
        legacy_file.write_bytes(b"legacy")
    legacy_segment = storage_path / str(uuid.uuid4())
    legacy_segment.mkdir()
    (legacy_segment / "data_level0.bin").write_bytes(b"legacy")
    unrelated_directory = storage_path / "not-a-segment"
    unrelated_directory.mkdir()
    uuid_named_file = storage_path / str(uuid.uuid4())
    uuid_named_file.write_bytes(b"not a segment")
    unrelated_file = storage_path / "notes.txt"
    unrelated_file.write_bytes(b"kept")

    assert await delete_collection(space, deleted.name)
    assert not deleted.exists()
    assert await delete_collection(space, deleted.name)

    cleanup_superseded_collections(
        space,
        preserved=frozenset({space.default_collection}),
        candidates_only=True,
    )

    assert list_collection_names(storage_path) == (space.default_collection,)
    assert [row["content"] for row in stored_rows(live)] == ["live"]
    assert not any(legacy_file.exists() for legacy_file in legacy_files)
    assert not legacy_segment.exists()
    assert unrelated_directory.is_dir()
    assert uuid_named_file.is_file()
    assert unrelated_file.read_bytes() == b"kept"
