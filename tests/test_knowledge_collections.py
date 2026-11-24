"""Knowledge collection lifecycle at the store boundary: writes, builds, deletion, cleanup, and cold lookups."""

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
from mindroom.knowledge.collection_lifetime import collection_lifetime_lock, read_collection
from mindroom.knowledge.collections import (
    CollectionSpace,
    KnowledgeChunk,
    cleanup_superseded_collections,
    open_collection,
    write_file_rows,
)
from mindroom.knowledge.manager import KnowledgeManager
from mindroom.knowledge.registry import (
    get_published_index,
    published_index_storage_path,
    resolve_published_index_key,
)
from mindroom.knowledge.utils import resolve_knowledge_base_access_async
from mindroom.knowledge.vector_store import CollectionMeta, KnowledgeCollection, StoredRow, list_collection_names
from tests.knowledge_test_support import (
    collection_meta,
    publish,
    published_collection,
    seed_collection,
    stored_rows,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.index_reader import PublishedIndexReader


@pytest.fixture
def space(tmp_path: Path) -> CollectionSpace:
    """An isolated collection space."""
    return CollectionSpace(
        base_id="docs",
        knowledge_path=tmp_path / "docs",
        storage_path=tmp_path / "storage",
    )


@pytest.mark.parametrize("deletion_fails", [False, True])
def test_superseded_cleanup_preserves_retained_reader(
    space: CollectionSpace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    deletion_fails: bool,
) -> None:
    """Cleanup keeps the preserved and unowned collections searchable and continues past a failed deletion."""
    retained_name = f"{space._default_collection}_candidate_current"
    retained = open_collection(space, retained_name)
    seed_collection(retained, [{"id": "document", "content": "document"}])
    for name in (space._default_collection, f"{space._default_collection}_candidate_stale"):
        seed_collection(open_collection(space, name), [])
    seed_collection(KnowledgeCollection(path=space.storage_path, name="unowned"), [])
    if deletion_fails:
        original_delete = KnowledgeCollection.delete

        def fail_one_deletion(self: KnowledgeCollection) -> None:
            if self.name == space._default_collection:
                message = "collection operation failed"
                raise OSError(message)
            original_delete(self)

        monkeypatch.setattr(KnowledgeCollection, "delete", fail_one_deletion)

    cleanup_superseded_collections(space, preserved=frozenset({retained_name}))

    assert [hit.content for hit in retained.search([1.0], limit=1)] == ["document"]
    expected = {retained_name, "unowned"}
    if deletion_fails:
        expected.add(space._default_collection)
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


def test_write_file_rows_replaces_one_files_rows_and_claims_it(space: CollectionSpace) -> None:
    """A write replaces its own file's rows and claim, keeps other files', and stores what search results show."""
    collection = open_collection(space, space._default_collection)
    collection.create(collection_meta())
    write_file_rows(collection, "other.md", _SIGNATURE, _rows(("other", [0.0, 1.0])))
    write_file_rows(
        collection,
        "notes.md",
        (0, 9, "old"),
        _rows(("stale", [1.0, 0.0]), ("stale", [1.0, 0.0]), ("stale", [1.0, 0.0])),
    )
    write_file_rows(collection, "notes.md", _SIGNATURE, _rows(("first", [1.0, 0.0]), ("null\x00byte", [0.5, 0.5])))

    rows = stored_rows(collection)
    stored = [row for row in rows if row["metadata"]["source_path"] == "notes.md"]
    assert sorted(row["content"] for row in stored) == ["first", "null�byte"]
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
    assert (metadata["source_mtime_ns"], metadata["source_size"], metadata["source_digest"]) == _SIGNATURE
    assert any(row["metadata"]["source_path"] == "other.md" for row in rows)
    assert collection.files() == {"notes.md": _SIGNATURE, "other.md": _SIGNATURE}


def _interrupted_pages() -> Iterator[Sequence[StoredRow]]:
    yield [StoredRow(id="new", document="new", metadata={"source_path": "new.md"}, embedding=[1.0, 0.0])]
    message = "collection operation failed"
    raise OSError(message)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("interrupted", ["while_filling", "at_the_rename"])
def test_a_build_interrupted_before_its_rename_leaves_no_partial_collection(
    space: CollectionSpace,
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing: bool,
    interrupted: str,
) -> None:
    """A build that stops before its rename leaves the old collection or none, and a leftover only cleanup removes."""
    name = f"{space._default_collection}_candidate_build"
    collection = open_collection(space, name)
    if existing:
        collection.create(collection_meta())
        write_file_rows(collection, "notes.md", _SIGNATURE, _rows(("old", [1.0, 0.0])))
    before = (stored_rows(collection), collection.files(), collection.meta()) if existing else None
    meta = collection_meta(rebuild=True)
    if interrupted == "at_the_rename":

        def fail_replace(_path: Path, _target: Path) -> Never:
            message = "collection operation failed"
            raise OSError(message)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", fail_replace)
            with pytest.raises(OSError, match="collection operation failed"):
                collection.create(meta)
    else:
        with pytest.raises(OSError, match="collection operation failed"):
            collection.create(meta, pages=_interrupted_pages())

    building = space.storage_path / f"{name}.sqlite3.building"
    assert building.is_file()
    if before is None:
        assert not collection.exists()
        assert list_collection_names(space.storage_path) == ()
    else:
        assert (stored_rows(collection), collection.files(), collection.meta()) == before

    cleanup_superseded_collections(space, preserved=frozenset({name}), candidates_only=True)

    assert not building.exists()
    assert collection.exists() is existing


def _docs_config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    """One knowledge base ``docs`` at ``tmp_path / "docs"``, stored under ``tmp_path / "storage"``."""
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
    return config, runtime_paths


@pytest.fixture
def published_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Config, RuntimePaths]:
    """A published real collection with no process-local read handle yet."""
    monkeypatch.setattr("mindroom.knowledge.registry._published_indexes", {})
    (tmp_path / "docs").mkdir()
    config, runtime_paths = _docs_config(tmp_path)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    seed_collection(KnowledgeCollection(path=published_index_storage_path(key), name="present"), [])
    publish(key, collection="present", indexed_count=0, source_signature="empty")
    return config, runtime_paths


def test_concurrent_cold_lookups_keep_returned_readers_queryable(
    published_lookup: tuple[Config, RuntimePaths],
) -> None:
    """Concurrent cold lookups each return a read handle that selects the published collection."""
    config, runtime_paths = published_lookup
    barrier = Barrier(2)

    def lookup() -> PublishedIndexReader:
        barrier.wait(timeout=10)
        result = get_published_index("docs", config=config, runtime_paths=runtime_paths)
        assert result.availability is KnowledgeAvailability.READY
        assert result.reader is not None
        return result.reader

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(lookup)
        second = pool.submit(lookup)
        readers = [first.result(timeout=10), second.result(timeout=10)]
    for reader in readers:
        with read_collection(reader.path, reader.settings) as selected:
            assert selected == "present"


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


class _LengthEmbedder:
    """Deterministic embedder, so a real refresh needs no provider."""

    sends_batches = True

    def embed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
        _ = purpose
        return [[float(len(text)), float(sum(map(ord, text)) % 97), 1.0] for text in texts]

    async def aembed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


@pytest.mark.asyncio
async def test_real_refreshes_resume_the_retired_collection_and_serve_exactly_the_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Against the real store, the collection a publication retired is resumed and written only where files changed."""
    monkeypatch.setattr("mindroom.knowledge.manager.create_configured_embedder", lambda *_a, **_k: _LengthEmbedder())
    copied: list[str] = []
    original_copy = KnowledgeCollection.create_copy

    def _counting_copy(self: KnowledgeCollection, source: KnowledgeCollection, meta: CollectionMeta) -> None:
        copied.append(source.name)
        original_copy(self, source, meta)

    monkeypatch.setattr(KnowledgeCollection, "create_copy", _counting_copy)
    docs = tmp_path / "docs"
    docs.mkdir()
    for index in range(6):
        (docs / f"doc{index}.md").write_text(f"body {index}", encoding="utf-8")
    config, runtime_paths = _docs_config(tmp_path)

    async def _refresh() -> str:
        manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
        assert (await manager.reindex_all()).published
        published = published_collection(manager)
        served = sorted((row["metadata"]["source_path"], row["content"]) for row in stored_rows(published))
        assert served == sorted((path.name, path.read_text(encoding="utf-8")) for path in docs.iterdir())
        assert set(published.files()) == {path.name for path in docs.iterdir()}
        return published.name

    cold = await _refresh()
    (docs / "doc0.md").write_text("first change", encoding="utf-8")
    second = await _refresh()
    assert copied == [cold], "the first refresh after a cold build copies the published index"
    copied.clear()
    (docs / "doc1.md").write_text("second change", encoding="utf-8")
    (docs / "doc2.md").unlink()
    (docs / "added.md").write_text("added body", encoding="utf-8")

    assert await _refresh() == cold
    assert copied == [], "a refresh resuming the retired collection copied the published index"
    (docs / "doc3.md").write_text("third change", encoding="utf-8")
    assert await _refresh() == second
    assert copied == []
