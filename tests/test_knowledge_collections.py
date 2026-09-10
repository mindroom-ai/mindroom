"""Real Chroma resource ownership at temporary knowledge collection boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from typing import TYPE_CHECKING, Never

import pytest
from agno.knowledge.embedder.base import Embedder
from chromadb.api.client import Client
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings

from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.candidate_checkpoint import CandidateCheckpoint
from mindroom.knowledge.collections import (
    CollectionSpace,
    build_vector_db,
    cleanup_superseded_collections,
    delete_collection,
    require_chroma_vector_db,
)
from mindroom.knowledge.index_metadata import PublishedIndexState, save_published_index_state
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.read_process import _read_slot
from mindroom.knowledge.registry import (
    _PublishedIndexHandle,
    get_published_index,
    published_index_metadata_path,
    published_index_storage_path,
    resolve_published_index_key,
)
from mindroom.knowledge.utils import resolve_knowledge_base_access

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


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


@pytest.fixture
def published_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Config, RuntimePaths]:
    """A published real collection with no process-local read handle yet."""
    monkeypatch.setattr("mindroom.knowledge.registry._published_indexes", {})
    docs = tmp_path / "docs"
    docs.mkdir()
    config = Config(
        agents={},
        models={},
        knowledge_bases={"docs": KnowledgeBaseConfig(path=str(docs))},
        memory={"embedder": {"provider": "openai", "config": {"api_key": "synthetic-test-key"}}},
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    storage_path = published_index_storage_path(key)
    with Client(settings=Settings(is_persistent=True, persist_directory=str(storage_path))) as seed:
        seed.create_collection("present")
    save_published_index_state(
        published_index_metadata_path(key),
        PublishedIndexState(
            settings=key.indexing_settings,
            status="complete",
            collection="present",
            indexed_count=0,
            source_signature="empty",
        ),
    )
    return config, runtime_paths


def test_concurrent_cold_lookups_keep_returned_readers_queryable(
    published_lookup: tuple[Config, RuntimePaths],
) -> None:
    """Concurrent published handles remain usable without retaining parent native clients."""
    config, runtime_paths = published_lookup
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    storage_path = published_index_storage_path(key)
    barrier = Barrier(2)

    def lookup() -> _PublishedIndexHandle:
        barrier.wait(timeout=10)
        result = get_published_index("docs", config=config, runtime_paths=runtime_paths)
        assert result.index is not None
        return result.index

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lookup)
        second = pool.submit(lookup)
        indexes = [first.result(timeout=10), second.result(timeout=10)]
    for index in indexes:
        assert index.knowledge.vector_db.exists() is True
    # A native client with different settings can open immediately: none of
    # the published handles retain a system in this process.
    with Client(settings=Settings(is_persistent=True, persist_directory=str(storage_path), allow_reset=True)) as client:
        assert client.count_collections() == 1


@pytest.mark.parametrize("cached", [False, True])
def test_busy_published_lookup_reports_unavailable_then_recovers(
    published_lookup: tuple[Config, RuntimePaths],
    cached: bool,
) -> None:
    """Reader saturation stays a per-base availability result, including cached handles."""
    config, runtime_paths = published_lookup
    if cached:
        assert resolve_knowledge_base_access("docs", config, runtime_paths).knowledge is not None

    with _read_slot(), _read_slot():
        busy = resolve_knowledge_base_access("docs", config, runtime_paths)
        assert busy.knowledge is None
        assert busy.availability is KnowledgeAvailability.REFRESH_FAILED

    recovered = resolve_knowledge_base_access("docs", config, runtime_paths)
    assert recovered.knowledge is not None
    assert recovered.availability is KnowledgeAvailability.READY


def test_cached_published_lookup_reports_probe_timeout(
    published_lookup: tuple[Config, RuntimePaths],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached probe timeout must not escape knowledge availability resolution."""
    config, runtime_paths = published_lookup
    assert resolve_knowledge_base_access("docs", config, runtime_paths).knowledge is not None

    def timeout(*args: object, **kwargs: object) -> Never:  # noqa: ARG001
        message = "Knowledge read timed out"
        raise TimeoutError(message)

    with monkeypatch.context() as patch:
        patch.setattr("mindroom.knowledge.read_proxy.collection_exists", timeout)
        unavailable = resolve_knowledge_base_access("docs", config, runtime_paths)
        assert unavailable.knowledge is None
        assert unavailable.availability is KnowledgeAvailability.REFRESH_FAILED

    assert resolve_knowledge_base_access("docs", config, runtime_paths).availability is KnowledgeAvailability.READY
