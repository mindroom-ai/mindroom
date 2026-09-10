"""Real Chroma resource ownership at knowledge collection cleanup boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never, cast

import pytest
from agno.knowledge.embedder.base import Embedder
from chromadb.api.client import Client
from chromadb.config import Settings

from mindroom.knowledge.collections import (
    CollectionSpace,
    build_vector_db,
    cleanup_superseded_collections,
    delete_collection,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def space(tmp_path: Path) -> CollectionSpace:
    """An isolated collection space with an embedder that cannot make requests."""
    return CollectionSpace(
        base_id="docs",
        knowledge_path=tmp_path / "docs",
        storage_path=tmp_path / "chroma",
        embedder_factory=Embedder,
    )


def _assert_storage_released(space: CollectionSpace, expected_collections: set[str]) -> None:
    # Different settings are accepted only after every previous client closes.
    with Client(
        settings=Settings(is_persistent=True, persist_directory=str(space.storage_path), allow_reset=True),
    ) as client:
        assert {collection.name for collection in client.list_collections()} == expected_collections


@pytest.mark.asyncio
@pytest.mark.parametrize("already_missing", [False, True])
async def test_delete_collection_releases_owned_client(space: CollectionSpace, *, already_missing: bool) -> None:
    """Both successful and already-absent deletion must release their temporary client."""
    with Client(settings=Settings(is_persistent=True, persist_directory=str(space.storage_path))) as seed:
        seed.create_collection("retained")
        if not already_missing:
            seed.create_collection(space.default_collection)

    assert await delete_collection(space, space.default_collection) is True

    _assert_storage_released(space, {"retained"})


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_fails", [False, True])
async def test_delete_collection_failure_releases_owned_client(
    space: CollectionSpace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe_fails: bool,
) -> None:
    """Deletion rejection and a failing follow-up probe must leave no unowned client."""
    with Client(settings=Settings(is_persistent=True, persist_directory=str(space.storage_path))) as seed:
        seed.create_collection(space.default_collection)

    def fail_operation(self: Client, name: str, **kwargs: object) -> Never:  # noqa: ARG001
        message = "collection operation failed"
        raise RuntimeError(message)

    monkeypatch.setattr(Client, "delete_collection", fail_operation)
    if probe_fails:
        monkeypatch.setattr(Client, "get_collection", fail_operation)

    assert await delete_collection(space, space.default_collection) is False

    _assert_storage_released(space, {space.default_collection})


@pytest.mark.parametrize("deletion_fails", [False, True])
def test_superseded_cleanup_releases_owned_clients_and_preserves_reader(
    space: CollectionSpace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deletion_fails: bool,
) -> None:
    """Cleanup must preserve its borrowed reader and release temporary clients even on failure."""
    retained_name = f"{space.default_collection}_candidate_current"
    stale_candidate = f"{space.default_collection}_candidate_stale"
    vector_db = build_vector_db(space, retained_name)
    client = cast("Client", vector_db.client)
    try:
        retained = client.create_collection(retained_name)
        retained.add(ids=["document"], embeddings=[[1.0, 0.0]])
        client.create_collection(space.default_collection)
        client.create_collection(stale_candidate)
        client.create_collection("unowned")
        if deletion_fails:
            original_delete = Client.delete_collection

            def fail_one_deletion(self: Client, name: str) -> None:
                if name == space.default_collection:
                    message = "collection deletion failed"
                    raise RuntimeError(message)
                original_delete(self, name=name)

            monkeypatch.setattr(Client, "delete_collection", fail_one_deletion)

        cleanup_superseded_collections(space, vector_db=vector_db, preserved=frozenset({retained_name}))

        assert retained.query(query_embeddings=[[1.0, 0.0]], n_results=1)["ids"] == [["document"]]
    finally:
        client.close()

    expected = {retained_name, "unowned"}
    if deletion_fails:
        expected.add(space.default_collection)
    _assert_storage_released(space, expected)
