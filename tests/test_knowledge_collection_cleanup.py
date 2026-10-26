"""Integration tests for physical knowledge collection cleanup."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.collections import (
    CollectionSpace,
    cleanup_superseded_collections,
    delete_collection,
    open_collection,
)
from mindroom.knowledge.index_metadata import save_published_index_state, state_for_publication
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.registry import get_published_index
from mindroom.knowledge.vector_store import list_collection_names
from tests.conftest import runtime_paths_for
from tests.knowledge_test_support import _config, patch_vector_store, seed_collection, stored_rows  # noqa: F401

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_cleanup_removes_legacy_chroma_storage_and_leaves_other_files(tmp_path: Path) -> None:
    """Cleanup reclaims superseded collections, and after a publication old Chroma files, preserving everything else."""
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
    interrupted_reset = storage_path / f"{space.default_collection}_candidate_crashed.sqlite3.building"
    interrupted_reset.write_bytes(b"half built")

    assert await delete_collection(space, deleted.name)
    assert not deleted.exists()
    assert await delete_collection(space, deleted.name)

    cleanup_superseded_collections(
        space,
        preserved=frozenset({space.default_collection}),
        candidates_only=True,
    )

    assert list_collection_names(storage_path) == (space.default_collection,)
    assert not interrupted_reset.exists()
    # Until a publication, the Chroma files stay: they are what a downgrade would read.
    assert all(legacy_file.exists() for legacy_file in legacy_files)
    assert legacy_segment.is_dir()

    cleanup_superseded_collections(space, preserved=frozenset({space.default_collection}))

    assert list_collection_names(storage_path) == (space.default_collection,)
    assert [row["content"] for row in stored_rows(live)] == ["live"]
    assert not any(legacy_file.exists() for legacy_file in legacy_files)
    assert not legacy_segment.exists()
    assert unrelated_directory.is_dir()
    assert uuid_named_file.is_file()
    assert unrelated_file.read_bytes() == b"kept"


@pytest.mark.asyncio
@pytest.mark.usefixtures("patch_vector_store")
async def test_a_chroma_era_index_rebuilds_and_its_files_go_only_after_publication(tmp_path: Path) -> None:
    """An index Chroma held reports a config mismatch, rebuilds in the owned store, and only then loses its files."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("Travel policy and hotel limits.", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    storage = manager._base_storage_path
    save_published_index_state(
        manager._indexing_settings_path,
        state_for_publication(
            settings=replace(manager._indexing_settings, vector_store=""),
            collection=manager._collections.default_collection,
            indexed_count=1,
            source_signature="chroma-era",
            published_revision=None,
        ),
    )
    (storage / "chroma.sqlite3").write_bytes(b"chroma")
    segment = storage / str(uuid.uuid4())
    segment.mkdir()

    assert get_published_index("docs", config=config, runtime_paths=runtime_paths).availability is (
        KnowledgeAvailability.CONFIG_MISMATCH
    )
    upgraded = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    assert upgraded.needs_full_reindex_on_create()

    outcome = await upgraded.reindex_all()

    assert outcome.published
    assert get_published_index("docs", config=config, runtime_paths=runtime_paths).availability is (
        KnowledgeAvailability.READY
    )
    assert not (storage / "chroma.sqlite3").exists()
    assert not segment.exists()
