"""Knowledge collection lifecycle at the store boundary: writes, resets, deletion, cleanup, and cold lookups."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING, Never

import pytest

from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.candidate_checkpoint import CandidateCheckpoint
from mindroom.knowledge.collection_lifetime import collection_lifetime_lock
from mindroom.knowledge.collections import (
    CollectionSpace,
    KnowledgeChunk,
    cleanup_superseded_collections,
    delete_collection,
    open_collection,
    write_source_rows,
)
from mindroom.knowledge.index_metadata import PublishedIndexState, save_published_index_state
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.registry import (
    _PublishedIndexHandle,
    get_published_index,
    published_index_metadata_path,
    published_index_storage_path,
    resolve_published_index_key,
)
from mindroom.knowledge.utils import resolve_knowledge_base_access_async
from mindroom.knowledge.vector_store import KnowledgeCollection, list_collection_names
from tests.knowledge_test_support import seed_collection, stored_rows

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


@pytest.fixture
def space(tmp_path: Path) -> CollectionSpace:
    """An isolated collection space."""
    return CollectionSpace(
        base_id="docs",
        knowledge_path=tmp_path / "docs",
        storage_path=tmp_path / "storage",
    )


def _fail_delete(self: KnowledgeCollection) -> Never:  # noqa: ARG001
    message = "collection operation failed"
    raise OSError(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("already_missing", [False, True])
async def test_delete_collection_reports_present_and_absent_collections_deleted(
    space: CollectionSpace,
    *,
    already_missing: bool,
) -> None:
    """Both successful and already-absent deletion report success and leave other collections alone."""
    retained = f"{space.default_collection}_candidate_retained"
    seed_collection(open_collection(space, retained), [{"content": "retained"}])
    if not already_missing:
        seed_collection(open_collection(space, space.default_collection), [{"content": "published"}])

    assert await delete_collection(space, space.default_collection) is True

    assert list_collection_names(space.storage_path) == (retained,)


@pytest.mark.asyncio
async def test_delete_collection_failure_keeps_the_collection(
    space: CollectionSpace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected deletion reports failure and keeps the collection."""
    seed_collection(open_collection(space, space.default_collection), [{"content": "published"}])
    monkeypatch.setattr(KnowledgeCollection, "delete", _fail_delete)

    assert await delete_collection(space, space.default_collection) is False

    assert list_collection_names(space.storage_path) == (space.default_collection,)


@pytest.mark.parametrize("deletion_fails", [False, True])
def test_superseded_cleanup_preserves_retained_reader(
    space: CollectionSpace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deletion_fails: bool,
) -> None:
    """Cleanup keeps the preserved and unowned collections searchable and continues past a failed deletion."""
    retained_name = f"{space.default_collection}_candidate_current"
    retained = open_collection(space, retained_name)
    seed_collection(retained, [{"id": "document", "content": "document"}])
    for name in (space.default_collection, f"{space.default_collection}_candidate_stale"):
        seed_collection(open_collection(space, name), [])
    seed_collection(KnowledgeCollection(path=space.storage_path, name="unowned"), [])
    if deletion_fails:
        original_delete = KnowledgeCollection.delete

        def fail_one_deletion(self: KnowledgeCollection) -> None:
            if self.name == space.default_collection:
                _fail_delete(self)
            original_delete(self)

        monkeypatch.setattr(KnowledgeCollection, "delete", fail_one_deletion)

    cleanup_superseded_collections(space, preserved=frozenset({retained_name}))

    assert [hit.content for hit in retained.search([1.0], limit=1)] == ["document"]
    expected = {retained_name, "unowned"}
    if deletion_fails:
        expected.add(space.default_collection)
    assert set(list_collection_names(space.storage_path)) == expected


_SIGNATURE = (1, 9, "digest")


def _rows(*texts_and_vectors: tuple[str, list[float]]) -> list[tuple[KnowledgeChunk, list[float]]]:
    return [
        (
            KnowledgeChunk(
                text=text,
                name="notes.md",
                metadata={"chunk": index, "chunk_size": len(text), "page": None},
            ),
            vector,
        )
        for index, (text, vector) in enumerate(texts_and_vectors, start=1)
    ]


def test_write_source_rows_replaces_one_files_rows(space: CollectionSpace) -> None:
    """A write replaces its own file's rows, keeps other files', and stores what search results show."""
    collection = open_collection(space, space.default_collection)
    collection.reset()
    write_source_rows(
        collection,
        relative_path="other.md",
        signature=_SIGNATURE,
        rows=_rows(("other", [0.0, 1.0])),
    )
    write_source_rows(
        collection,
        relative_path="notes.md",
        signature=(0, 9, "old"),
        rows=_rows(("stale", [1.0, 0.0]), ("stale", [1.0, 0.0]), ("stale", [1.0, 0.0])),
    )
    write_source_rows(
        collection,
        relative_path="notes.md",
        signature=_SIGNATURE,
        rows=_rows(("first", [1.0, 0.0]), ("null\x00byte", [0.5, 0.5])),
    )

    rows = stored_rows(collection)
    stored = [row for row in rows if row["metadata"]["source_path"] == "notes.md"]
    assert sorted(row["content"] for row in stored) == ["first", "null\ufffdbyte"]
    metadata = sorted((row["metadata"] for row in stored), key=lambda row: row["chunk"])[0]
    assert set(metadata) == {
        "chunk",
        "chunk_size",
        "content_hash",
        "content_id",
        "linked_to",
        "name",
        "source_digest",
        "source_mtime_ns",
        "source_path",
        "source_size",
    }
    assert (metadata["name"], metadata["linked_to"], metadata["chunk_size"]) == ("notes.md", "", 5)
    assert any(row["metadata"]["source_path"] == "other.md" for row in rows)


def test_reset_raises_and_keeps_the_old_rows_when_the_swap_fails(
    space: CollectionSpace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reset that cannot swap in the empty file raises and leaves the old rows, never a half-built collection."""
    collection = open_collection(space, space.default_collection)
    collection.reset()
    write_source_rows(
        collection,
        relative_path="notes.md",
        signature=_SIGNATURE,
        rows=_rows(("old", [1.0, 0.0])),
    )

    def fail_replace(_path: Path, _target: Path) -> Never:
        msg = "collection operation failed"
        raise OSError(msg)

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="collection operation failed"):
        collection.reset()
    monkeypatch.undo()
    assert [row["content"] for row in stored_rows(collection)] == ["old"]


def _candidate_manager(tmp_path: Path) -> KnowledgeManager:
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
    return KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)


@pytest.mark.parametrize(
    ("candidate_state", "expected"),
    [("missing", False), ("empty", False), ("unclaimed", True), ("claimed", False)],
)
def test_candidate_inspection_reports_unclaimed_rows(tmp_path: Path, candidate_state: str, *, expected: bool) -> None:
    """Only a non-empty candidate without completed claims reports an interrupted bulk copy."""
    manager = _candidate_manager(tmp_path)
    candidate = open_collection(manager._collections, f"{manager._collections.default_collection}_candidate_test")
    if candidate_state != "missing":
        seed_collection(candidate, [] if candidate_state == "empty" else [{"content": "document"}])
    checkpoint = CandidateCheckpoint(
        collection=candidate.name,
        settings=manager._indexing_settings,
        completed={"document.md": (1, 1, "digest")} if candidate_state == "claimed" else {},
    )

    assert manager._candidate_holds_unclaimed_rows(checkpoint) is expected


def test_candidate_inspection_error_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed candidate row lookup must propagate to the caller."""
    manager = _candidate_manager(tmp_path)
    candidate = open_collection(manager._collections, f"{manager._collections.default_collection}_candidate_test")
    seed_collection(candidate, [])
    checkpoint = CandidateCheckpoint(collection=candidate.name, settings=manager._indexing_settings)

    def fail_has_rows(self: KnowledgeCollection) -> Never:  # noqa: ARG001
        message = "candidate row lookup failed"
        raise RuntimeError(message)

    monkeypatch.setattr(KnowledgeCollection, "has_rows", fail_has_rows)
    with pytest.raises(RuntimeError, match="candidate row lookup failed"):
        manager._candidate_holds_unclaimed_rows(checkpoint)


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
    seed_collection(KnowledgeCollection(path=storage_path, name="present"), [])
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
    """Concurrent cold lookups each return a published handle that still finds its collection."""
    config, runtime_paths = published_lookup
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
        assert index.reader.exists() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_published_lookup_is_not_blocked_by_a_running_search(
    published_lookup: tuple[Config, RuntimePaths],
    cached: bool,
) -> None:
    """A search holding its shared collection lease does not make fresh published metadata unavailable."""
    config, runtime_paths = published_lookup
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    if cached:
        assert (
            await resolve_knowledge_base_access_async("docs", config, runtime_paths, execution_identity=None)
        ).knowledge is not None

    with collection_lifetime_lock(published_index_storage_path(key), exclusive=False):
        available = await resolve_knowledge_base_access_async("docs", config, runtime_paths, execution_identity=None)
    assert available.knowledge is not None
    assert available.availability is KnowledgeAvailability.READY


@pytest.mark.asyncio
async def test_cached_published_lookup_reports_probe_failure(
    published_lookup: tuple[Config, RuntimePaths],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached probe failure must not escape knowledge availability resolution."""
    config, runtime_paths = published_lookup
    assert (
        await resolve_knowledge_base_access_async("docs", config, runtime_paths, execution_identity=None)
    ).knowledge is not None

    def fail_probe(self: KnowledgeCollection) -> Never:  # noqa: ARG001
        message = "Knowledge probe failed"
        raise OSError(message)

    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeCollection, "exists", fail_probe)
        unavailable = await resolve_knowledge_base_access_async("docs", config, runtime_paths, execution_identity=None)
        assert unavailable.knowledge is None
        assert unavailable.availability is KnowledgeAvailability.REFRESH_FAILED

    assert (
        await resolve_knowledge_base_access_async("docs", config, runtime_paths, execution_identity=None)
    ).availability is KnowledgeAvailability.READY
