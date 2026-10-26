"""Published readers survive refresh without crossing storage or embedding scopes."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
from typing import TYPE_CHECKING

import pytest

from mindroom.knowledge import collections, index_reader, registry
from mindroom.knowledge.collections import (
    CollectionSpace,
    candidate_collection_name,
    cleanup_superseded_collections,
    open_collection,
)
from mindroom.knowledge.index_metadata import (
    load_published_index_state,
    save_published_index_state,
    state_for_publication,
)
from mindroom.knowledge.index_reader import PublishedIndexReader
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from tests.conftest import runtime_paths_for
from tests.knowledge_test_support import _config, seed_collection

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path


class _Embedder:
    sends_batches = False

    def embed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
        del purpose
        return [[1.0] for _text in texts]

    async def aembed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


@pytest.fixture
def publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[CollectionSpace, registry.PublishedIndexKey, PublishedIndexReader]:
    """Build the production reader with one real, published collection."""
    config = _config(tmp_path, bases={"docs": tmp_path / "docs"}, agent_bases=["docs"])
    paths = runtime_paths_for(config)
    key = registry.resolve_published_index_key("docs", config=config, runtime_paths=paths)
    space = CollectionSpace("docs", tmp_path / "docs", registry.published_index_storage_path(key))
    collection = _publish(space, key, "original")
    state = state_for_publication(
        settings=key.indexing_settings,
        collection=collection,
        indexed_count=1,
        source_signature="original",
        published_revision=None,
    )
    monkeypatch.setattr(registry, "create_configured_embedder", lambda *_args: _Embedder())
    reader = registry._build_published_index_reader(key, state, config=config, runtime_paths=paths)
    assert isinstance(reader, PublishedIndexReader)
    return space, key, reader


def _publish(space: CollectionSpace, key: registry.PublishedIndexKey, content: str) -> str:
    name = candidate_collection_name(space)
    seed_collection(open_collection(space, name), [{"id": content, "content": content}])
    save_published_index_state(
        registry.published_index_metadata_path(key),
        state_for_publication(
            settings=key.indexing_settings,
            collection=name,
            indexed_count=1,
            source_signature=content,
            published_revision=None,
        ),
    )
    cleanup_superseded_collections(space, preserved=frozenset({name}))
    return name


@pytest.mark.asyncio
async def test_retained_reader_follows_refresh(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, PublishedIndexReader],
) -> None:
    """An agent created before multiple swaps queries the current publication."""
    space, key, reader = publication
    _publish(space, key, "replacement")
    _publish(space, key, "latest")
    documents = await reader.asearch("alpha", limit=5)
    assert [document.content for document in documents] == ["latest"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["embedder_model", "embedder_dimensions", "knowledge_path", "storage_root", "base_id", "include_patterns"],
)
async def test_retained_reader_rejects_changed_scope_or_embeddings(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, PublishedIndexReader],
    field: str,
) -> None:
    """A newer collection must not redirect the old requester or query vector."""
    space, key, reader = publication
    name = _publish(space, key, "replacement")
    settings = replace(key.indexing_settings, **{field: "changed"})
    save_published_index_state(
        registry.published_index_metadata_path(key),
        state_for_publication(
            settings=settings,
            collection=name,
            indexed_count=1,
            source_signature="replacement",
            published_revision=None,
        ),
    )
    with pytest.raises(ValueError, match="unavailable or incompatible"):
        await reader.asearch("alpha", limit=5)


@pytest.mark.asyncio
async def test_retained_reader_accepts_new_chunk_settings(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, PublishedIndexReader],
) -> None:
    """Chunk boundaries do not change vector or requester compatibility."""
    space, key, reader = publication
    _publish(space, replace(key, indexing_settings=replace(key.indexing_settings, chunk_size="2000")), "replacement")
    assert [document.content for document in await reader.asearch("alpha", limit=5)] == ["replacement"]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, "corrupt", "indexing"])
async def test_retained_reader_does_not_hide_unavailable_publication(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, PublishedIndexReader],
    metadata: str | None,
) -> None:
    """Loss of the authoritative publication fails explicitly, even if old vectors exist."""
    _space, key, reader = publication
    path = registry.published_index_metadata_path(key)
    if metadata is None:
        path.unlink()
    elif metadata == "corrupt":
        path.write_text("invalid", encoding="utf-8")
    else:
        state = load_published_index_state(path)
        assert state is not None
        save_published_index_state(path, replace(state, status="indexing"))
    with pytest.raises(ValueError, match="unavailable or incompatible"):
        await reader.asearch("alpha", limit=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("direct_delete", [False, True])
async def test_cleanup_waits_for_selected_reader_and_releases_after_read(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, PublishedIndexReader],
    monkeypatch: pytest.MonkeyPatch,
    direct_delete: bool,
) -> None:
    """A read's selection is leased across publication and deletion until the search finishes."""
    space, key, reader = publication
    selected: list[str] = []
    selection_made = Event()
    release = Event()
    original_read_collection = index_reader.read_collection

    @contextmanager
    def gated_read_collection(path: str, collection: str, settings: Mapping[str, str] | None) -> Iterator[str]:
        with original_read_collection(path, collection, settings) as selection:
            selected.append(selection)
            selection_made.set()
            release.wait()
            yield selection

    cleanup_started = Event()
    original_lock = collections.collection_lifetime_lock

    @contextmanager
    def observed_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
        cleanup_started.set()
        with original_lock(path, exclusive=exclusive):
            yield

    monkeypatch.setattr(index_reader, "read_collection", gated_read_collection)
    monkeypatch.setattr(collections, "collection_lifetime_lock", observed_lock)
    read_task = asyncio.create_task(reader.asearch("alpha", limit=5))
    refresh_task = None
    try:
        assert await asyncio.to_thread(selection_made.wait, 10)
        assert selected == [reader.collection_name]
        refresh_task = asyncio.create_task(
            asyncio.to_thread(collections._delete_collection_sync, space, reader.collection_name)
            if direct_delete
            else asyncio.to_thread(_publish, space, key, "replacement"),
        )
        assert await asyncio.to_thread(cleanup_started.wait, 10)
        assert not refresh_task.done()
        assert open_collection(space, reader.collection_name).exists()
        release.set()
        assert [document.content for document in await read_task] == ["original"]
        await asyncio.wait_for(refresh_task, 10)
        assert not open_collection(space, reader.collection_name).exists()
        if direct_delete:
            await asyncio.to_thread(_publish, space, key, "replacement")
        monkeypatch.setattr(index_reader, "read_collection", original_read_collection)
        assert [document.content for document in await reader.asearch("alpha", limit=5)] == ["replacement"]
    finally:
        release.set()
        await asyncio.gather(read_task, return_exceptions=True)
        if refresh_task is not None:
            await asyncio.wait_for(refresh_task, 10)


@pytest.mark.asyncio
async def test_refresh_resolution_stays_in_requester_storage(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, PublishedIndexReader],
    tmp_path: Path,
) -> None:
    """Identical base/source identities in another requester root cannot redirect reads."""
    space, key, reader = publication
    other_root = str(tmp_path / "another-requester")
    other_key = replace(
        key,
        storage_root=other_root,
        indexing_settings=replace(key.indexing_settings, storage_root=other_root),
    )
    other_space = replace(space, storage_path=registry.published_index_storage_path(other_key))
    _publish(space, key, "own-replacement")
    _publish(other_space, other_key, "other-private-content")
    assert [document.content for document in await reader.asearch("alpha", limit=5)] == ["own-replacement"]


@pytest.mark.asyncio
async def test_knowledge_initialization_follows_refresh(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, PublishedIndexReader],
) -> None:
    """An agent's knowledge handle follows the same publication as its reader's searches."""
    space, key, reader = publication
    _publish(space, key, "replacement")
    source = KnowledgeSource(description=KnowledgeSourceDescription(base_id="docs", description=""), index=reader)
    knowledge = AgentKnowledge(sources=(source,))
    assert [hit.content for hit in await knowledge.asearch("alpha")] == ["replacement"]


@pytest.mark.asyncio
async def test_registry_initialization_accepts_superseded_state_snapshot(
    publication: tuple[CollectionSpace, registry.PublishedIndexKey, PublishedIndexReader],
    tmp_path: Path,
) -> None:
    """A refresh between metadata loading and handle construction cannot hide memory."""
    space, key, _reader = publication
    state = load_published_index_state(registry.published_index_metadata_path(key))
    assert state is not None
    _publish(space, key, "replacement")
    config = _config(tmp_path, bases={"docs": tmp_path / "docs"}, agent_bases=["docs"])
    reader = registry._load_queryable_index_from_state(
        key,
        state,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    assert reader is not None
    assert [hit.content for hit in await reader.asearch("alpha", limit=5)] == ["replacement"]
