"""Knowledge snapshot and refresh behavior tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock

import pytest
from agno.knowledge.document.base import Document
from fastapi.testclient import TestClient

import mindroom.knowledge.utils as knowledge_utils
from mindroom.api import main
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.knowledge import (
    KnowledgeAvailability,
    PerBindingKnowledgeRefreshOwner,
    clear_published_snapshots,
    get_agent_knowledge,
    get_published_snapshot,
    redact_url_credentials,
    refresh_knowledge_binding,
    snapshot_indexed_count,
)
from mindroom.knowledge.manager import KnowledgeManager, knowledge_source_signature
from mindroom.knowledge.registry import load_published_indexing_state, resolve_snapshot_key, snapshot_metadata_path
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Collection:
    def __init__(self, name: str) -> None:
        self._name = name

    def get(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        include: list[str] | None = None,
        where: dict[str, object] | None = None,
    ) -> dict[str, object]:
        _ = include
        with _VectorDb.lock:
            selected_all = list(_VectorDb.collections.get(self._name, []))
        if where:
            key, value = next(iter(where.items()))
            selected_all = [item for item in selected_all if item["metadata"].get(key) == value]
        selected = selected_all[offset:] if limit is None else selected_all[offset : offset + limit]
        ids = [str(index) for index in range(offset, offset + len(selected))]
        return {"ids": ids, "metadatas": [dict(item["metadata"]) for item in selected]}


class _Client:
    def get_collection(self, name: str) -> _Collection:
        return _Collection(name)


class _VectorDb:
    collections: ClassVar[dict[str, list[dict[str, object]]]] = {}
    lock: ClassVar[Lock] = Lock()

    def __init__(self, *, collection: str, **_: object) -> None:
        self.collection_name = collection
        self.client = _Client()

    def delete(self) -> bool:
        with self.lock:
            self.collections.pop(self.collection_name, None)
        return True

    def create(self) -> None:
        with self.lock:
            self.collections[self.collection_name] = []

    def exists(self) -> bool:
        with self.lock:
            return self.collection_name in self.collections

    def search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, object] | list[object] | None = None,
    ) -> list[Document]:
        _ = (query, filters)
        with self.lock:
            items = list(self.collections.get(self.collection_name, []))
        return [Document(content=str(item["content"]), meta_data=dict(item["metadata"])) for item in items[:limit]]


class _Knowledge:
    def __init__(self, vector_db: _VectorDb) -> None:
        self.vector_db = vector_db

    async def ainsert(
        self,
        *,
        path: str,
        metadata: dict[str, object],
        upsert: bool,
        reader: object | None = None,
    ) -> None:
        _ = (upsert, reader)
        with _VectorDb.lock:
            _VectorDb.collections.setdefault(self.vector_db.collection_name, []).append(
                {"content": Path(path).read_text(encoding="utf-8"), "metadata": dict(metadata)},
            )

    def remove_vectors_by_metadata(self, metadata: dict[str, object]) -> bool:
        with _VectorDb.lock:
            items = _VectorDb.collections.get(self.vector_db.collection_name, [])
            filtered = [
                item for item in items if not all(item["metadata"].get(key) == value for key, value in metadata.items())
            ]
            _VectorDb.collections[self.vector_db.collection_name] = filtered
        return len(filtered) != len(items)

    def search(self, query: str, max_results: int | None = None) -> list[Document]:
        return self.vector_db.search(query=query, limit=max_results or 5)


@pytest.fixture(autouse=True)
def patch_vector_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use an in-memory vector store for knowledge snapshot tests."""
    _VectorDb.collections = {}
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _VectorDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _Knowledge)
    monkeypatch.setattr("mindroom.knowledge.manager._create_embedder", lambda *_args, **_kwargs: object())
    clear_published_snapshots()
    knowledge_utils._refresh_scheduled_at.clear()
    yield
    clear_published_snapshots()
    knowledge_utils._refresh_scheduled_at.clear()
    _VectorDb.collections = {}


def _config(
    tmp_path: Path,
    *,
    bases: dict[str, Path],
    agent_bases: list[str],
    git_configs: dict[str, KnowledgeGitConfig] | None = None,
) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=agent_bases)},
            models={},
            knowledge_bases={
                base_id: KnowledgeBaseConfig(
                    path=str(path),
                    watch=False,
                    git=(git_configs or {}).get(base_id),
                )
                for base_id, path in bases.items()
            },
        ),
        runtime_paths,
    )


def _publish_api_config(api_app: object, config: Config) -> None:
    context = main._app_context(api_app)
    context.config_data = config.authored_model_dump()
    context.runtime_config = config
    context.config_load_result = main.ConfigLoadResult(success=True)


def test_missing_shared_knowledge_schedules_refresh_and_returns_none(tmp_path: Path) -> None:
    """A missing published snapshot is advisory and schedules only the referenced base."""
    config = _config(
        tmp_path,
        bases={"docs": tmp_path / "docs", "unused": tmp_path / "unused"},
        agent_bases=["docs"],
    )
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()

    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths_for(config),
        refresh_owner=owner,
    )

    assert knowledge is None
    owner.schedule_initial_load.assert_called_once()
    assert owner.schedule_initial_load.call_args.args == ("docs",)
    assert owner.schedule_initial_load.call_args.kwargs["config"] is config
    assert owner.schedule_refresh.call_count == 0


@pytest.mark.asyncio
async def test_ready_snapshot_access_does_not_refresh_unchanged_sources(tmp_path: Path) -> None:
    """A ready snapshot is returned immediately without churn when sources are unchanged."""
    docs_path = tmp_path / "docs"
    unused_path = tmp_path / "unused"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("ready snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path, "unused": unused_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()

    knowledge = get_agent_knowledge("helper", config, runtime_paths, refresh_owner=owner)
    second_knowledge = get_agent_knowledge("helper", config, runtime_paths, refresh_owner=owner)

    assert knowledge is not None
    assert second_knowledge is not None
    assert [document.content for document in knowledge.search("snapshot", max_results=5)] == ["ready snapshot"]
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_not_called()
    assert len(_VectorDb.collections) == 1


@pytest.mark.asyncio
async def test_ready_snapshot_access_schedules_refresh_when_sources_change(tmp_path: Path) -> None:
    """Ready access schedules only when a cheap source signature detects local changes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("ready snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("ready snapshot changed", encoding="utf-8")
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()

    knowledge = get_agent_knowledge("helper", config, runtime_paths, refresh_owner=owner)
    second_knowledge = get_agent_knowledge("helper", config, runtime_paths, refresh_owner=owner)

    assert knowledge is not None
    assert second_knowledge is not None
    assert [document.content for document in knowledge.search("snapshot", max_results=5)] == ["ready snapshot"]
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()
    assert owner.schedule_refresh.call_args.args == ("docs",)


@pytest.mark.asyncio
async def test_existing_published_snapshot_is_used_while_refresh_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow refresh builds a candidate while readers continue using the last-good snapshot."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / "doc.md").write_text("new snapshot", encoding="utf-8")

    started = asyncio.Event()
    release = asyncio.Event()
    original_index_file_locked = KnowledgeManager._index_file_locked

    async def _block_candidate(
        self: KnowledgeManager,
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> bool:
        if knowledge is not None and knowledge is not self.get_knowledge() and not started.is_set():
            started.set()
            await release.wait()
        return await original_index_file_locked(
            self,
            resolved_path,
            upsert=upsert,
            knowledge=knowledge,
            indexed_files=indexed_files,
            indexed_signatures=indexed_signatures,
        )

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _block_candidate)
    refresh_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await started.wait()

    knowledge = get_agent_knowledge("helper", config, runtime_paths)
    assert knowledge is not None
    assert [document.content for document in knowledge.search("snapshot", max_results=5)] == ["old snapshot"]

    release.set()
    await refresh_task
    knowledge = get_agent_knowledge("helper", config, runtime_paths)
    assert knowledge is not None
    assert [document.content for document in knowledge.search("snapshot", max_results=5)] == ["new snapshot"]


@pytest.mark.asyncio
async def test_same_physical_binding_refreshes_are_serialized_across_config_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh writes are serialized by physical storage target, not settings-sensitive snapshot key."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    runtime_paths = runtime_paths_for(config)
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    active_refreshes = 0
    max_active_refreshes = 0
    call_count = 0

    async def _blocked_reindex(self: KnowledgeManager) -> int:
        _ = self
        nonlocal active_refreshes, max_active_refreshes, call_count
        active_refreshes += 1
        max_active_refreshes = max(max_active_refreshes, active_refreshes)
        call_count += 1
        try:
            if call_count == 1:
                first_entered.set()
                await release_first.wait()
            else:
                second_entered.set()
            return 0
        finally:
            active_refreshes -= 1

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _blocked_reindex)

    first_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await first_entered.wait()
    second_task = asyncio.create_task(
        refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths),
    )
    await asyncio.sleep(0)

    assert not second_entered.is_set()
    assert max_active_refreshes == 1

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert second_entered.is_set()
    assert max_active_refreshes == 1


@pytest.mark.asyncio
async def test_published_snapshot_handle_survives_later_refresh_generations(tmp_path: Path) -> None:
    """Already-returned read handles remain valid across later refresh generations."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("generation one", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    first_lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert first_lookup.snapshot is not None
    first_knowledge = first_lookup.snapshot.knowledge
    first_collection = first_knowledge.vector_db.collection_name

    doc.write_text("generation two", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("generation three", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert first_collection in _VectorDb.collections
    assert [document.content for document in first_knowledge.search("generation", max_results=5)] == ["generation one"]
    latest = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert latest.snapshot is not None
    assert [document.content for document in latest.snapshot.knowledge.search("generation", max_results=5)] == [
        "generation three",
    ]


@pytest.mark.asyncio
async def test_successful_refreshes_keep_bounded_collection_generations(tmp_path: Path) -> None:
    """Repeated publishes retain only the latest bounded generation set."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("generation 0", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    for generation in range(6):
        doc.write_text(f"generation {generation}", encoding="utf-8")
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert len(_VectorDb.collections) <= 3
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.snapshot is not None
    assert [document.content for document in lookup.snapshot.knowledge.search("generation", max_results=5)] == [
        "generation 5",
    ]


@pytest.mark.asyncio
async def test_failed_refresh_preserves_last_good_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed candidate build marks stale availability but keeps serving the old collection."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stable snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    (docs_path / "doc.md").write_text("broken refresh", encoding="utf-8")
    original_index_file_locked = KnowledgeManager._index_file_locked

    async def _fail_candidate(
        self: KnowledgeManager,
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> bool:
        if knowledge is not None and knowledge is not self.get_knowledge():
            msg = "candidate failed"
            raise RuntimeError(msg)
        return await original_index_file_locked(
            self,
            resolved_path,
            upsert=upsert,
            knowledge=knowledge,
            indexed_files=indexed_files,
            indexed_signatures=indexed_signatures,
        )

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _fail_candidate)
    with pytest.raises(RuntimeError, match="candidate failed"):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    unavailable: dict[str, KnowledgeAvailability] = {}
    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
    )

    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert knowledge is not None
    assert [document.content for document in knowledge.search("snapshot", max_results=5)] == ["stable snapshot"]


@pytest.mark.asyncio
async def test_embedder_config_mismatch_returns_no_incompatible_snapshot(tmp_path: Path) -> None:
    """An embedder-changing config mismatch should not query old vectors with the new embedder."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old embedder snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    changed_config = config.model_copy(deep=True)
    changed_config.memory.embedder.config.model = "text-embedding-3-large"
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    knowledge = get_agent_knowledge(
        "helper",
        changed_config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
        refresh_owner=owner,
    )

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda config: setattr(config.knowledge_bases["docs"].git, "repo_url", "https://example.com/other/repo.git"),
        lambda config: setattr(config.knowledge_bases["docs"].git, "branch", "release"),
        lambda config: setattr(config.knowledge_bases["docs"].git, "include_patterns", ["other/**"]),
        lambda config: setattr(config.knowledge_bases["docs"].git, "exclude_patterns", ["doc.md"]),
        lambda config: setattr(config.knowledge_bases["docs"].git, "skip_hidden", False),
        lambda config: setattr(config.knowledge_bases["docs"], "include_extensions", [".txt"]),
        lambda config: setattr(config.knowledge_bases["docs"], "exclude_extensions", [".md"]),
    ],
)
async def test_corpus_changing_config_mismatch_returns_no_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
) -> None:
    """Source identity and membership filter changes must not serve old content."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old corpus snapshot", encoding="utf-8")
    git_config = KnowledgeGitConfig(
        repo_url="https://example.com/org/repo.git",
        include_patterns=["**/*.md"],
        skip_hidden=True,
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_success(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        assert index_changes is False
        self._git_last_successful_commit = "rev-a"
        return {"updated": True, "changed_count": 1, "removed_count": 0}

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync_success)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    changed_config = config.model_copy(deep=True)
    mutate(changed_config)
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    knowledge = get_agent_knowledge(
        "helper",
        changed_config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
        refresh_owner=owner,
    )

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_failed_refresh_after_config_change_preserves_published_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed candidate refresh must not rewrite last-good metadata to the attempted settings."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stable snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    old_key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    old_state = load_published_indexing_state(snapshot_metadata_path(old_key))
    assert old_state is not None

    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024

    async def _fail_candidate(
        self: KnowledgeManager,
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> bool:
        _ = (self, resolved_path, upsert, knowledge, indexed_files, indexed_signatures)
        msg = "candidate failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _fail_candidate)
    with pytest.raises(RuntimeError, match="candidate failed"):
        await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)

    changed_key = resolve_snapshot_key("docs", config=changed_config, runtime_paths=runtime_paths)
    preserved_state = load_published_indexing_state(snapshot_metadata_path(changed_key))
    assert preserved_state is not None
    assert preserved_state.settings == old_state.settings
    assert preserved_state.collection == old_state.collection
    assert preserved_state.availability == KnowledgeAvailability.REFRESH_FAILED.value

    lookup = get_published_snapshot("docs", config=changed_config, runtime_paths=runtime_paths)
    assert lookup.snapshot is not None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED


def test_stale_metadata_without_collection_returns_unavailable_snapshot(tmp_path: Path) -> None:
    """Metadata alone must not create or expose an empty ready collection."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = snapshot_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "settings": list(key.indexing_settings),
                "status": "complete",
                "collection": "missing_collection",
                "availability": "ready",
            },
        ),
        encoding="utf-8",
    )

    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert lookup.snapshot is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    assert "missing_collection" not in _VectorDb.collections


@pytest.mark.asyncio
async def test_first_time_partial_refresh_does_not_publish_ready_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold refresh with incomplete file indexing must not become a last-good snapshot."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "good.md").write_text("good", encoding="utf-8")
    (docs_path / "bad.md").write_text("bad", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    original_index_file_locked = KnowledgeManager._index_file_locked

    async def _skip_bad_file(
        self: KnowledgeManager,
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> bool:
        if resolved_path.name == "bad.md":
            return False
        return await original_index_file_locked(
            self,
            resolved_path,
            upsert=upsert,
            knowledge=knowledge,
            indexed_files=indexed_files,
            indexed_signatures=indexed_signatures,
        )

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _skip_bad_file)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert result.indexed_count == 1
    assert result.published is False
    assert lookup.snapshot is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED


@pytest.mark.asyncio
async def test_embedder_changing_partial_refresh_does_not_publish_old_snapshot_under_new_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial refresh cannot cache old incompatible vectors under a new snapshot key."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("old embedder snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("new embedder candidate", encoding="utf-8")
    changed_config = config.model_copy(deep=True)
    changed_config.memory.embedder.config.model = "text-embedding-3-large"

    async def _partial_candidate(
        self: KnowledgeManager,
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> bool:
        _ = (self, resolved_path, upsert, knowledge, indexed_files, indexed_signatures)
        return False

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _partial_candidate)

    result = await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)
    lookup = get_published_snapshot("docs", config=changed_config, runtime_paths=runtime_paths)

    assert result.indexed_count == 0
    assert result.published is False
    assert lookup.snapshot is None
    assert lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH


@pytest.mark.asyncio
async def test_cold_refresh_exception_surfaces_failed_availability_and_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cold refresh failures remain visible and do not reschedule on every access."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("broken", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    async def _raise_reindex(self: KnowledgeManager) -> int:
        _ = self
        msg = "cold refresh failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _raise_reindex)
    with pytest.raises(RuntimeError, match="cold refresh failed"):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.snapshot is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED

    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    first = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
        refresh_owner=owner,
    )
    second = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
        refresh_owner=owner,
    )

    assert first is None
    assert second is None
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_api_delete_removes_published_vectors_before_background_refresh(tmp_path: Path) -> None:
    """DELETE success means the deleted source is no longer queryable immediately."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("delete me now", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    before_delete = get_agent_knowledge("helper", config, runtime_paths)
    assert before_delete is not None
    assert [document.content for document in before_delete.search("delete", max_results=5)] == ["delete me now"]

    owner = MagicMock()
    owner.schedule_refresh = MagicMock()
    main.app.state.knowledge_refresh_owner = owner
    client = TestClient(main.app)

    response = client.delete("/api/knowledge/bases/docs/files/guide.md")

    assert response.status_code == 200
    after_delete = get_agent_knowledge("helper", config, runtime_paths)
    assert after_delete is not None
    assert after_delete.search("delete", max_results=5) == []
    owner.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_owner_runs_independent_per_binding_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduling one binding does not replace, cancel, or wait for another binding."""
    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    config = _config(tmp_path, bases={"a": docs_a, "b": docs_b}, agent_bases=["a", "b"])
    runtime_paths = runtime_paths_for(config)
    owner = PerBindingKnowledgeRefreshOwner()
    started: list[str] = []
    release: dict[str, asyncio.Event] = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def _fake_refresh(base_id: str, **_kwargs: object) -> object:
        started.append(base_id)
        await release[base_id].wait()
        if base_id == "a":
            msg = "a failed"
            raise RuntimeError(msg)
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_owner.refresh_knowledge_binding", _fake_refresh)

    owner.schedule_refresh("a", config=config, runtime_paths=runtime_paths)
    owner.schedule_refresh("a", config=config, runtime_paths=runtime_paths)
    owner.schedule_refresh("b", config=config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)

    assert sorted(started) == ["a", "b"]
    assert len(owner._tasks) == 2
    release["b"].set()
    await asyncio.sleep(0)
    assert any(key.base_id == "a" for key in owner._tasks)
    release["a"].set()
    await owner.shutdown()


@pytest.mark.asyncio
async def test_refresh_owner_runs_one_pending_refresh_after_active_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schedules received during an active refresh run once more after the active task."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    owner = PerBindingKnowledgeRefreshOwner()
    started_count = 0
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()

    async def _fake_refresh(base_id: str, **_kwargs: object) -> object:
        _ = base_id
        nonlocal started_count
        started_count += 1
        if started_count == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_owner.refresh_knowledge_binding", _fake_refresh)

    owner.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await first_started.wait()
    owner.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    owner.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)

    assert started_count == 1

    release_first.set()
    await second_started.wait()
    await owner.shutdown()

    assert started_count == 2


@pytest.mark.asyncio
async def test_refresh_owner_shutdown_suppresses_completed_refresh_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown drains fire-and-forget refresh task failures instead of re-raising them."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    owner = PerBindingKnowledgeRefreshOwner()

    async def _fake_refresh(base_id: str, **_kwargs: object) -> object:
        _ = base_id
        msg = "refresh failed"
        raise RuntimeError(msg)

    monkeypatch.setattr("mindroom.knowledge.refresh_owner.refresh_knowledge_binding", _fake_refresh)

    owner.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)
    await owner.shutdown()


@pytest.mark.asyncio
async def test_refresh_owner_does_not_schedule_after_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late schedule calls after shutdown do not create orphaned refresh tasks."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    owner = PerBindingKnowledgeRefreshOwner()
    calls = 0

    async def _fake_refresh(base_id: str, **_kwargs: object) -> object:
        _ = base_id
        nonlocal calls
        calls += 1
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_owner.refresh_knowledge_binding", _fake_refresh)

    await owner.shutdown()
    owner.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)

    assert calls == 0
    assert owner._tasks == {}


def test_snapshot_key_is_per_binding_not_raw_base_id(tmp_path: Path) -> None:
    """The same base id resolves to separate refresh keys when storage binding differs."""
    path = tmp_path / "docs"
    config_a = _config(tmp_path / "a", bases={"docs": path}, agent_bases=["docs"])
    config_b = _config(tmp_path / "b", bases={"docs": path}, agent_bases=["docs"])

    key_a = get_published_snapshot("docs", config=config_a, runtime_paths=runtime_paths_for(config_a)).key
    key_b = get_published_snapshot("docs", config=config_b, runtime_paths=runtime_paths_for(config_b)).key

    assert key_a.base_id == key_b.base_id == "docs"
    assert key_a != key_b


@pytest.mark.asyncio
async def test_snapshot_indexed_count_uses_persisted_metadata_without_collection_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routine status counts come from metadata rather than scanning vector rows."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.snapshot is not None

    def _raise_scan(self: _Client, name: str) -> _Collection:
        _ = (self, name)
        msg = "collection scan should not be used"
        raise AssertionError(msg)

    monkeypatch.setattr(_Client, "get_collection", _raise_scan)

    assert snapshot_indexed_count(lookup.snapshot) == 1


@pytest.mark.asyncio
async def test_git_refresh_syncs_before_reindex_and_publishes_revision_without_secret_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed refresh syncs first, publishes the revision, and persists no URL userinfo."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git snapshot", encoding="utf-8")
    git_config = KnowledgeGitConfig(
        repo_url="https://ghp_secret:x-oauth-basic@example.com/org/repo.git",
        branch="main",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    order: list[str] = []
    original_reindex = KnowledgeManager.reindex_all

    async def _sync_success(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        assert index_changes is False
        order.append("sync")
        self._git_last_successful_commit = "rev-git"
        return {"updated": True, "changed_count": 1, "removed_count": 0}

    async def _track_reindex(self: KnowledgeManager) -> int:
        order.append("reindex")
        return await original_reindex(self)

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync_success)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_indexing_state(snapshot_metadata_path(key))
    metadata_text = snapshot_metadata_path(key).read_text(encoding="utf-8")

    assert result.published is True
    assert order == ["sync", "reindex"]
    assert state is not None
    assert state.published_revision == "rev-git"
    assert state.source_signature == knowledge_source_signature(config, "docs", docs_path)
    assert "ghp_secret" not in metadata_text
    assert "x-oauth-basic" not in metadata_text


@pytest.mark.asyncio
async def test_git_sync_failure_preserves_last_good_snapshot_and_redacts_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git sync failure keeps the last-good snapshot available under stale metadata."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stable git snapshot", encoding="utf-8")
    git_config = KnowledgeGitConfig(
        repo_url="https://ghp_secret:x-oauth-basic@example.com/org/repo.git",
        branch="main",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_success(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        assert index_changes is False
        self._git_last_successful_commit = "rev-ok"
        return {"updated": True, "changed_count": 1, "removed_count": 0}

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync_success)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    async def _sync_failure(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        _ = (self, index_changes)
        msg = "fetch failed https://ghp_secret:x-oauth-basic@example.com/org/repo.git"
        raise RuntimeError(msg)

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync_failure)
    with pytest.raises(RuntimeError, match="fetch failed"):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_indexing_state(snapshot_metadata_path(key))
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert state is not None
    assert state.availability == KnowledgeAvailability.REFRESH_FAILED.value
    assert state.last_error is not None
    assert "ghp_secret" not in state.last_error
    assert "x-oauth-basic" not in state.last_error
    assert lookup.snapshot is not None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    assert [document.content for document in lookup.snapshot.knowledge.search("snapshot", max_results=5)] == [
        "stable git snapshot",
    ]


@pytest.mark.asyncio
async def test_cold_git_sync_failure_records_failed_availability_and_redacted_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first Git failure is observable as refresh_failed instead of initializing."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    git_config = KnowledgeGitConfig(
        repo_url="https://ghp_secret:x-oauth-basic@example.com/org/repo.git",
        branch="main",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_failure(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        _ = (self, index_changes)
        msg = "clone failed https://ghp_secret:x-oauth-basic@example.com/org/repo.git"
        raise RuntimeError(msg)

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync_failure)

    with pytest.raises(RuntimeError, match="clone failed"):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_indexing_state(snapshot_metadata_path(key))
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert state is not None
    assert state.availability == KnowledgeAvailability.REFRESH_FAILED.value
    assert state.last_error is not None
    assert "ghp_secret" not in state.last_error
    assert "x-oauth-basic" not in state.last_error
    assert lookup.snapshot is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED


def test_redact_url_credentials_hides_entire_http_userinfo() -> None:
    """Knowledge Git URL redaction must not leak token usernames."""
    assert redact_url_credentials("https://user:password@example.com/repo.git") == "https://***@example.com/repo.git"
    assert redact_url_credentials("https://ghp_secret:x-oauth-basic@example.com/repo.git") == (
        "https://***@example.com/repo.git"
    )
    assert redact_url_credentials("https://username@example.com/repo.git") == "https://***@example.com/repo.git"
    assert redact_url_credentials("ssh://git@example.com/repo.git") == "ssh://git@example.com/repo.git"
