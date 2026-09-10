"""Real Chroma resource ownership at temporary knowledge collection boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Never

import pytest
from agno.knowledge.embedder.base import Embedder
from chromadb.api.client import Client
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings

from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge.candidate_checkpoint import CandidateCheckpoint
from mindroom.knowledge.collections import (
    CollectionSpace,
    build_vector_db,
    cleanup_superseded_collections,
    delete_collection,
    require_chroma_vector_db,
)
from mindroom.knowledge.manager import KnowledgeManager

if TYPE_CHECKING:
    from collections.abc import Iterator
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
    client = vector_db.client
    assert isinstance(client, Client)
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


@contextmanager
def _candidate_manager(tmp_path: Path) -> Iterator[KnowledgeManager]:
    config = Config(
        agents={},
        models={},
        knowledge_bases={"docs": KnowledgeBaseConfig(path=str(tmp_path / "docs"))},
        memory={"embedder": {"provider": "openai", "config": {"api_key": "synthetic-test-key"}}},
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    try:
        yield manager
    finally:
        client = require_chroma_vector_db(manager._knowledge).client
        assert isinstance(client, Client)
        client.close()


@pytest.mark.parametrize(
    ("candidate_state", "expected"),
    [("missing", False), ("empty", False), ("unclaimed", True), ("claimed", False)],
)
def test_candidate_inspection_releases_owned_client(tmp_path: Path, candidate_state: str, *, expected: bool) -> None:
    """Every candidate-shape result must release only the temporary inspection client."""
    with _candidate_manager(tmp_path) as manager:
        space = manager._collections
        reader = require_chroma_vector_db(manager._knowledge).client
        candidate_name = f"{space.default_collection}_candidate_test"
        expected_collections = {space.default_collection}
        if candidate_state != "missing":
            candidate = reader.create_collection(candidate_name)
            expected_collections.add(candidate_name)
            if candidate_state != "empty":
                candidate.add(ids=["document"], embeddings=[[1.0, 0.0]])
        checkpoint = CandidateCheckpoint(
            collection=candidate_name,
            settings=manager._indexing_settings,
            completed={"document.md": (1, 1, "digest")} if candidate_state == "claimed" else {},
        )

        assert manager._candidate_holds_unclaimed_rows(checkpoint, embedder=Embedder()) is expected
        assert reader.get_collection(space.default_collection).count() == 0

    _assert_storage_released(space, expected_collections)


def test_candidate_inspection_error_releases_owned_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed candidate row lookup must propagate and release the inspection client."""
    with _candidate_manager(tmp_path) as manager:
        space = manager._collections
        reader = require_chroma_vector_db(manager._knowledge).client
        candidate_name = f"{space.default_collection}_candidate_test"
        reader.create_collection(candidate_name)
        checkpoint = CandidateCheckpoint(collection=candidate_name, settings=manager._indexing_settings)

        def fail_get(self: Collection, **kwargs: object) -> Never:  # noqa: ARG001
            message = "candidate row lookup failed"
            raise RuntimeError(message)

        monkeypatch.setattr(Collection, "get", fail_get)
        with pytest.raises(RuntimeError, match="candidate row lookup failed"):
            manager._candidate_holds_unclaimed_rows(checkpoint, embedder=Embedder())
        assert reader.get_collection(space.default_collection).count() == 0

    _assert_storage_released(space, {space.default_collection, candidate_name})
