"""Regressions for knowledge index state that had more than one owner.

A reverted publication, a refresh limit each scheduler applied alone, and an unchanged refresh that built the
local embedder to test that a file exists.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

import mindroom.knowledge.corpus_signature as knowledge_corpus_signature
import mindroom.knowledge.index_metadata as knowledge_index_metadata
import mindroom.knowledge.registry as knowledge_registry
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.memory import MemorySearchConfig
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.file_indexer import FileIndexer, _PreparedFile
from mindroom.knowledge.index_metadata import Publication, load_publication, save_publication
from mindroom.knowledge.refresh_runner import _refresh_knowledge_binding as refresh_knowledge_binding
from mindroom.knowledge.registry import (
    get_published_index,
    mark_knowledge_source_changed_async,
    published_index_storage_path,
    resolve_published_index_key,
)
from mindroom.knowledge.vector_store import KnowledgeCollection
from mindroom.memory._semantic_file_search import schedule_semantic_file_memory_refresh
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.knowledge_test_support import install_knowledge_embedder

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class _Embedder:
    sends_batches = False

    def embed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        del purpose
        return [[float(len(text)), 1.0] for text in texts]

    async def aembed(self, texts: Sequence[str], *, purpose: str) -> list[list[float]]:
        return self.embed(texts, purpose=purpose)


@pytest.fixture
def embedder_builds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Count every configured-embedder construction, wherever knowledge code builds one."""
    builds: list[str] = []
    embedder = _Embedder()

    def _build(*_args: object, **_kwargs: object) -> _Embedder:
        builds.append("built")
        return embedder

    install_knowledge_embedder(monkeypatch, _build)
    return builds


def _config(tmp_path: Path, bases: Sequence[str] = ("docs",)) -> Config:
    for base_id in bases:
        (tmp_path / base_id).mkdir(exist_ok=True)
    return bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=list(bases))},
            knowledge_bases={base_id: KnowledgeBaseConfig(path=str(tmp_path / base_id)) for base_id in bases},
        ),
        test_runtime_paths(tmp_path),
    )


@pytest.mark.asyncio
async def test_a_source_change_marked_during_a_publication_never_reverts_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedder_builds: list[str],
) -> None:
    """A watcher that reads the index state before a publication and writes after it leaves the new publication live."""
    del embedder_builds
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    docs = tmp_path / "docs"
    (docs / "a.md").write_text("alpha one", encoding="utf-8")
    (docs / "b.md").write_text("bravo one", encoding="utf-8")
    original_index = FileIndexer._index_file

    async def _fail_b(self: FileIndexer, prepared: _PreparedFile, **kwargs: object) -> object:
        if prepared.path.name == "b.md":
            return None
        return await original_index(self, prepared, **kwargs)

    monkeypatch.setattr(FileIndexer, "_index_file", _fail_b)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    monkeypatch.setattr(FileIndexer, "_index_file", original_index)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    first = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert first.publication is not None
    first_collection = first.publication.collection

    (docs / "a.md").write_text("alpha two", encoding="utf-8")
    loop = asyncio.get_running_loop()
    original_write = knowledge_index_metadata.write_json_atomic
    published_meanwhile: list[bool] = []

    def _publish_before_the_mark_writes(path: Path, payload: object) -> None:
        if not published_meanwhile:
            published_meanwhile.append(True)
            asyncio.run_coroutine_threadsafe(
                refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths),
                loop,
            ).result()
        original_write(path, payload)

    monkeypatch.setattr(knowledge_index_metadata, "write_json_atomic", _publish_before_the_mark_writes)
    await mark_knowledge_source_changed_async("docs", config=config, runtime_paths=runtime_paths, reason="watch")
    monkeypatch.setattr(knowledge_index_metadata, "write_json_atomic", original_write)

    assert published_meanwhile
    resolution = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert resolution.publication is not None
    assert resolution.publication.collection is not None
    assert resolution.publication.collection != first_collection
    assert resolution.reader is not None
    storage = published_index_storage_path(
        resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths),
    )
    assert KnowledgeCollection(path=storage, name=resolution.publication.collection).exists()
    hits = await resolution.reader.asearch("alpha", limit=5)
    assert [hit.content for hit in hits if "alpha" in hit.content] == ["alpha two"]


@pytest.mark.asyncio
async def test_one_refresh_child_runs_at_a_time_across_the_runtime_api_and_file_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the default limit of 1, a scheduled refresh, an API reindex, and a file-memory refresh run one by one."""
    config = _config(tmp_path, bases=("docs", "api"))
    runtime_paths = runtime_paths_for(config)
    memory_root = tmp_path / "memory_root"
    (memory_root / "memory").mkdir(parents=True)
    active = 0
    most_active = 0
    first_started = asyncio.Event()
    release = asyncio.Event()

    async def _refresh(_base_id: str, **_kwargs: object) -> object:
        nonlocal active, most_active
        active += 1
        most_active = max(most_active, active)
        first_started.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_runner.refresh_knowledge_binding_in_subprocess", _refresh)
    scheduler = _MultiAgentOrchestrator(runtime_paths=runtime_paths, config=config).knowledge_refresh_scheduler
    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    reindex = asyncio.create_task(
        scheduler.refresh_now("api", config=config, runtime_paths=runtime_paths, force_reindex=True),
    )
    assert schedule_semantic_file_memory_refresh(
        scope_user_id="alice",
        root=memory_root,
        config=config,
        runtime_paths=runtime_paths,
        search_config=MemorySearchConfig(mode="semantic"),
    )
    await first_started.wait()
    for _ in range(20):
        await asyncio.sleep(0)

    try:
        assert most_active == 1
    finally:
        release.set()
        await reindex
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_an_unchanged_refresh_builds_no_embedder(tmp_path: Path, embedder_builds: list[str]) -> None:
    """A refresh that finds the corpus unchanged republishes without constructing the embedder."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    (tmp_path / "docs" / "a.md").write_text("alpha one", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    embedder_builds.clear()

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published
    assert embedder_builds == []


@pytest.mark.asyncio
async def test_a_refresh_that_only_restamps_files_builds_no_embedder(
    tmp_path: Path,
    embedder_builds: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout or sync that rewrites only timestamps changes the corpus hash but leaves nothing to embed."""
    # Files written just now sit inside the racy window, whose stamps never count; close it so the stamps change.
    monkeypatch.setattr(knowledge_corpus_signature, "_RACY_WINDOW_NS", 0)
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    path = tmp_path / "docs" / "a.md"
    path.write_text("alpha one", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns + 10**10, stat.st_mtime_ns + 10**10))
    embedder_builds.clear()

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published
    assert embedder_builds == []


@pytest.mark.asyncio
async def test_a_refresh_of_only_empty_files_builds_no_embedder(tmp_path: Path, embedder_builds: list[str]) -> None:
    """An empty file is claimed without vectors, so a refresh that indexes only empty files has nothing to embed."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    (tmp_path / "docs" / "empty.md").write_text("", encoding="utf-8")

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published
    assert embedder_builds == []


@pytest.mark.asyncio
async def test_a_publication_landing_during_a_lookup_keeps_the_base_searchable(
    tmp_path: Path,
    embedder_builds: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh that publishes and deletes the old collection mid-lookup leaves the lookup a reader of the new one."""
    del embedder_builds
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    (tmp_path / "docs" / "a.md").write_text("alpha one", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    storage = published_index_storage_path(key)
    old = load_publication(storage)
    assert old is not None
    assert old.collection is not None
    replacement = KnowledgeCollection(path=storage, name=f"{old.collection[:-4]}beef")
    shutil.copyfile(KnowledgeCollection(path=storage, name=old.collection).file, replacement.file)
    exists = knowledge_registry.published_collection_exists

    def _exists_then_replaced(lookup_key: object, publication: Publication) -> bool:
        found = exists(lookup_key, publication)
        if publication.collection == old.collection:
            save_publication(storage, replace(old, collection=replacement.name))
            KnowledgeCollection(path=storage, name=old.collection).delete()
        return found

    monkeypatch.setattr(knowledge_registry, "published_collection_exists", _exists_then_replaced)

    resolution = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert resolution.reader is not None
    assert resolution.availability is KnowledgeAvailability.READY
    assert [document.content for document in await resolution.reader.asearch("alpha", limit=1)] == ["alpha one"]


def test_a_config_change_replaces_the_bindings_cached_read_handle(tmp_path: Path, embedder_builds: list[str]) -> None:
    """Each settings change gives a binding a new key, so only the newest key keeps a cached handle."""
    del embedder_builds
    (tmp_path / "docs").mkdir()
    for chunk_size in (500, 600, 700):
        config = bind_runtime_paths(
            Config(
                agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=["docs"])},
                knowledge_bases={"docs": KnowledgeBaseConfig(path=str(tmp_path / "docs"), chunk_size=chunk_size)},
            ),
            test_runtime_paths(tmp_path),
        )
        key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths_for(config))
        knowledge_registry._cached_reader(key, config=config, runtime_paths=runtime_paths_for(config))

    assert list(knowledge_registry._published_indexes) == [key]


@pytest.mark.asyncio
async def test_a_retired_forced_rebuild_is_taken_over_as_an_ordinary_build(
    tmp_path: Path,
    embedder_builds: list[str],
) -> None:
    """Once a forced rebuild has published and been retired, taking it over is no rebuild and keeps its successor."""
    del embedder_builds
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    docs = tmp_path / "docs"
    (docs / "a.md").write_text("alpha one", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths, force_reindex=True)
    storage = published_index_storage_path(
        resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths),
    )
    rebuilt = load_publication(storage)
    assert rebuilt is not None
    (docs / "a.md").write_text("alpha two", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    successor = load_publication(storage)
    assert successor is not None
    assert successor.retired == rebuilt.collection

    (docs / "a.md").write_text("alpha three", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    latest = load_publication(storage)
    assert latest is not None
    assert (latest.collection, latest.retired) == (rebuilt.collection, successor.collection)
    assert latest.collection is not None
    assert not KnowledgeCollection(path=storage, name=latest.collection).meta().rebuild
