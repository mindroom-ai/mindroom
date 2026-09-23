"""Integration tests for physical Chroma collection cleanup."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from mindroom.knowledge.collections import (
    CollectionSpace,
    build_vector_db,
    cleanup_superseded_collections,
    delete_collection,
)

if TYPE_CHECKING:
    from pathlib import Path

    from chromadb.api import ClientAPI


def _create_collection_segment(storage_path: Path, client: ClientAPI, collection_name: str) -> Path:
    """Create one populated collection and return its new physical segment directory."""
    before = {path for path in storage_path.iterdir() if path.is_dir()}
    collection = client.create_collection(collection_name, embedding_function=None)
    collection.add(ids=["one"], embeddings=[[0.1, 0.2, 0.3]])
    created = {path for path in storage_path.iterdir() if path.is_dir()} - before
    assert len(created) == 1
    return created.pop()


@pytest.mark.asyncio
async def test_delete_collection_reclaims_only_unreferenced_segment_directories(tmp_path: Path) -> None:
    """Deleting a collection removes old orphans while preserving every live segment."""
    storage_path = tmp_path / "chroma"
    space = CollectionSpace(
        base_id="docs",
        knowledge_path=tmp_path / "docs",
        storage_path=storage_path,
        embedder_factory=MagicMock,
    )
    live_vector_db = build_vector_db(space, space.default_collection)
    client = live_vector_db.client
    live_segment = _create_collection_segment(storage_path, client, space.default_collection)
    unrelated_directory = storage_path / "not-a-segment"
    unrelated_directory.mkdir()

    stale_name = f"{space.default_collection}_candidate_stale"
    stale_segment = _create_collection_segment(storage_path, client, stale_name)
    client.delete_collection(stale_name)
    assert stale_segment.is_dir()

    deleted_name = f"{space.default_collection}_candidate_deleted"
    deleted_segment = _create_collection_segment(storage_path, client, deleted_name)

    assert await delete_collection(space, deleted_name)
    assert live_segment.is_dir()
    assert client.get_collection(space.default_collection, embedding_function=None).count() == 1
    assert unrelated_directory.is_dir()
    assert not stale_segment.exists()
    assert not deleted_segment.exists()

    later_stale_name = f"{space.default_collection}_candidate_later_stale"
    later_stale_segment = _create_collection_segment(storage_path, client, later_stale_name)
    client.delete_collection(later_stale_name)
    cleanup_superseded_collections(
        space,
        vector_db=live_vector_db,
        preserved=frozenset({space.default_collection}),
        candidates_only=True,
    )
    assert not later_stale_segment.exists()
    assert live_segment.is_dir()
