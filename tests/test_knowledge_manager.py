"""Knowledge index and refresh behavior tests."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
from pathlib import Path
from threading import Event, Lock, get_ident
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock

import pytest
from agno.knowledge.document.base import Document
from fastapi.testclient import TestClient
from watchfiles import Change

import mindroom.knowledge.manager as knowledge_manager_module
import mindroom.knowledge.refresh_runner as knowledge_refresh_runner
import mindroom.knowledge.refresh_scheduler as knowledge_refresh_scheduler
import mindroom.knowledge.registry as knowledge_registry
import mindroom.knowledge.utils as knowledge_utils
from mindroom.api import config_lifecycle, main
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentPrivateKnowledgeConfig
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.knowledge import (
    KnowledgeAvailability,
    KnowledgeAvailabilityDetail,
    KnowledgeRefreshScheduler,
    resolve_agent_knowledge_access,
)
from mindroom.knowledge.manager import (
    KnowledgeManager,
    git_checkout_present,
    knowledge_source_signature,
    list_git_tracked_knowledge_files,
    list_knowledge_files,
)
from mindroom.knowledge.redaction import credential_free_url_identity, redact_url_credentials
from mindroom.knowledge.refresh_runner import knowledge_binding_mutation_lock, refresh_knowledge_binding
from mindroom.knowledge.registry import (
    clear_published_indexes,
    get_published_index,
    load_published_index_state,
    published_index_metadata_path,
    published_index_refresh_state,
    published_indexed_count,
    resolve_published_index_key,
)
from mindroom.knowledge.watch import KnowledgeSourceWatcher
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine, Iterator


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

    def list_collections(self) -> list[str]:
        with _VectorDb.lock:
            return sorted(_VectorDb.collections)


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

    async def async_search(
        self,
        *,
        query: str,
        limit: int,
        filters: dict[str, object] | list[object] | None = None,
    ) -> list[Document]:
        return self.search(query=query, limit=limit, filters=filters)


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


class _AutoCreatingKnowledge(_Knowledge):
    def __init__(self, vector_db: _VectorDb) -> None:
        super().__init__(vector_db)
        if not vector_db.exists():
            vector_db.create()


@pytest.fixture(autouse=True)
def patch_vector_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Use an in-memory vector store for published knowledge index tests."""
    _VectorDb.collections = {}
    monkeypatch.setattr("mindroom.knowledge.manager.ChromaDb", _VectorDb)
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _Knowledge)
    monkeypatch.setattr("mindroom.knowledge.manager._create_embedder", lambda *_args, **_kwargs: object())
    clear_published_indexes()
    knowledge_utils._refresh_scheduled_at.clear()
    knowledge_refresh_runner._refresh_locks.clear()
    knowledge_refresh_runner._active_refresh_counts.clear()
    yield
    clear_published_indexes()
    knowledge_utils._refresh_scheduled_at.clear()
    knowledge_refresh_runner._refresh_locks.clear()
    knowledge_refresh_runner._active_refresh_counts.clear()
    _VectorDb.collections = {}


async def _wait_for_refresh_lock_borrowers(
    key: knowledge_registry.KnowledgeSourceRoot,
    expected: int,
) -> None:
    for _ in range(50):
        entry = knowledge_refresh_runner._refresh_locks.get(key)
        if entry is not None and entry.borrowers == expected:
            return
        await asyncio.sleep(0)
    pytest.fail(f"refresh lock for {key} did not reach {expected} borrowers")


def _create_idle_refresh_lock(key: knowledge_registry.KnowledgeSourceRoot) -> None:
    entry = knowledge_refresh_runner._borrow_refresh_lock_for_key(key)
    knowledge_refresh_runner._release_refresh_lock_for_key(key, entry)


def _config(
    tmp_path: Path,
    *,
    bases: dict[str, Path],
    agent_bases: list[str],
    git_configs: dict[str, KnowledgeGitConfig] | None = None,
    watch: bool = False,
) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", knowledge_bases=agent_bases)},
            models={},
            knowledge_bases={
                base_id: KnowledgeBaseConfig(
                    path=str(path),
                    watch=watch,
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


def _refresh_state_for_key(key: knowledge_registry.PublishedIndexKey) -> str:
    metadata_path = published_index_metadata_path(key)
    return knowledge_registry.published_index_refresh_state(
        load_published_index_state(metadata_path),
        metadata_exists=metadata_path.exists(),
    )


def _identity(requester_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="helper",
        requester_id=requester_id,
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session",
    )


def _set_git_tracked_files(manager: KnowledgeManager, *relative_paths: str) -> None:
    manager._git_tracked_relative_paths = set(relative_paths)


def _git_manager(
    tmp_path: Path,
    *,
    lfs: bool = False,
    sync_timeout_seconds: int = 3600,
) -> KnowledgeManager:
    knowledge_path = tmp_path / "knowledge"
    config = _config(
        tmp_path,
        bases={"docs": knowledge_path},
        agent_bases=["docs"],
        git_configs={
            "docs": KnowledgeGitConfig(
                repo_url="https://example.com/org/repo.git",
                branch="main",
                lfs=lfs,
                sync_timeout_seconds=sync_timeout_seconds,
            ),
        },
    )
    return KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))


def test_cold_git_status_with_existing_non_checkout_dir_returns_empty_files(tmp_path: Path) -> None:
    """Cold Git status should not run git ls-files before the checkout exists."""
    knowledge_path = tmp_path / "knowledge"
    knowledge_path.mkdir()
    config = _config(
        tmp_path,
        bases={"docs": knowledge_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git")},
    )
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))

    assert manager.list_files() == []
    assert manager._git_repo_present is False
    assert not (knowledge_path / ".git").exists()


@pytest.mark.asyncio
async def test_git_manager_construction_does_not_probe_checkout_on_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git checkout detection during construction must stay filesystem-only."""
    loop_thread_id = get_ident()
    knowledge_path = tmp_path / "knowledge"
    (knowledge_path / ".git").mkdir(parents=True)
    config = _config(
        tmp_path,
        bases={"docs": knowledge_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git")},
    )

    def _unexpected_checkout_probe(*_args: object, **_kwargs: object) -> bool:
        msg = f"git checkout probe ran during manager construction on thread {loop_thread_id}"
        raise AssertionError(msg)

    monkeypatch.setattr(knowledge_manager_module, "git_checkout_present", _unexpected_checkout_probe)

    await asyncio.sleep(0)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths_for(config))

    assert manager._git_repo_present is True


def test_missing_shared_knowledge_schedules_refresh_and_returns_none(tmp_path: Path) -> None:
    """A missing published index schedules only the referenced base."""
    config = _config(
        tmp_path,
        bases={"docs": tmp_path / "docs", "unused": tmp_path / "unused"},
        agent_bases=["docs"],
    )
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()

    knowledge = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths_for(config),
        refresh_scheduler=scheduler,
    ).knowledge

    assert knowledge is None
    scheduler.schedule_refresh.assert_called_once()
    assert scheduler.schedule_refresh.call_args.args == ("docs",)
    assert scheduler.schedule_refresh.call_args.kwargs["config"] is config


def test_initializing_knowledge_skips_duplicate_initial_load_when_scheduler_is_active(tmp_path: Path) -> None:
    """An active scheduler refresh is enough for initializing knowledge."""
    config = _config(
        tmp_path,
        bases={"docs": tmp_path / "docs", "unused": tmp_path / "unused"},
        agent_bases=["docs"],
    )
    runtime_paths = runtime_paths_for(config)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=True)
    scheduler.schedule_refresh = MagicMock()

    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler).knowledge

    assert knowledge is None
    scheduler.is_refreshing.assert_called_once()
    scheduler.schedule_refresh.assert_not_called()


def test_real_refresh_scheduler_without_running_loop_does_not_mark_active(tmp_path: Path) -> None:
    """Synchronous callers should not leave a binding stuck refreshing when no event loop is running."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()

    assert (
        resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler).knowledge is None
    )
    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    refresh_target = knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths)

    assert knowledge_refresh_runner.is_refresh_active(refresh_target) is False
    assert scheduler.is_refreshing("docs", config=config, runtime_paths=runtime_paths) is False


def test_refresh_scheduler_module_exports_one_concrete_scheduler_name() -> None:
    """The refresh scheduler module should expose one concrete scheduler concept."""
    assert knowledge_refresh_scheduler.KnowledgeRefreshScheduler.__name__ == "KnowledgeRefreshScheduler"
    assert not hasattr(knowledge_refresh_scheduler, "StandaloneKnowledgeRefreshScheduler")
    assert not hasattr(knowledge_refresh_scheduler, "OrchestratorKnowledgeRefreshScheduler")
    assert not hasattr(knowledge_refresh_scheduler, "PerBindingKnowledgeRefreshScheduler")


def test_failed_notice_without_index_says_unavailable() -> None:
    """Cold failed knowledge must not be described as stale when no index is attached."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.REFRESH_FAILED,
                search_available=False,
            ),
        },
    )

    assert notice is not None
    assert "unavailable for semantic search this turn" in notice
    assert "may be stale" not in notice
    assert "Do not claim to have searched it." in notice


def test_config_mismatch_notice_without_index_says_unavailable() -> None:
    """Cold config-mismatched knowledge must not imply stale semantic search occurred."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.CONFIG_MISMATCH,
                search_available=False,
            ),
        },
    )

    assert notice is not None
    assert "unavailable for semantic search this turn" in notice
    assert "may be stale" not in notice
    assert "Do not claim to have searched it." in notice


def test_stale_notice_without_index_says_unavailable() -> None:
    """Stale metadata without a loadable index must not imply semantic search occurred."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.STALE,
                search_available=False,
            ),
        },
    )

    assert notice is not None
    assert "unavailable for semantic search this turn" in notice
    assert "may be stale" not in notice
    assert "Do not claim to have searched it." in notice


@pytest.mark.asyncio
async def test_ready_index_access_does_not_refresh_unchanged_sources(tmp_path: Path) -> None:
    """A ready index is returned immediately without churn when sources are unchanged."""
    docs_path = tmp_path / "docs"
    unused_path = tmp_path / "unused"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("ready index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path, "unused": unused_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()

    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler).knowledge
    second_knowledge = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    ).knowledge

    assert knowledge is not None
    assert second_knowledge is not None
    assert [document.content for document in knowledge.search("index", max_results=5)] == ["ready index"]
    scheduler.schedule_refresh.assert_not_called()
    assert len(_VectorDb.collections) == 1


@pytest.mark.asyncio
async def test_shared_local_watch_index_refreshes_on_access_without_blocking_read(tmp_path: Path) -> None:
    """Shared local bases with watch=true schedule refresh on access while serving last-good content."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("shared local old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("shared local new", encoding="utf-8")
    scheduler = KnowledgeRefreshScheduler()

    try:
        knowledge = resolve_agent_knowledge_access(
            "helper",
            config,
            runtime_paths,
            refresh_scheduler=scheduler,
        ).knowledge
        assert knowledge is not None
        assert [document.content for document in knowledge.search("shared", max_results=5)] == ["shared local old"]

        for _attempt in range(50):
            await asyncio.sleep(0.01)
            refreshed = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
            if refreshed is not None and [
                document.content for document in refreshed.search("shared", max_results=5)
            ] == ["shared local new"]:
                break
        else:
            pytest.fail("background on-access refresh did not publish the edited local source")
    finally:
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_shared_local_watch_schedule_refresh_on_access_is_throttled(tmp_path: Path) -> None:
    """A freshly refreshed local watch=true base stays READY during refresh-on-access cooldown."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("shared local old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    unavailable_details: dict[str, KnowledgeAvailabilityDetail] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    assert _resolution.knowledge is not None
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    assert unavailable_details == {
        "docs": KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.STALE,
            search_available=True,
        ),
    }

    doc.write_text("shared local new", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    unavailable.clear()
    unavailable_details.clear()
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    refreshed_knowledge = _resolution.knowledge

    assert refreshed_knowledge is not None
    assert [document.content for document in refreshed_knowledge.search("shared", max_results=5)] == [
        "shared local new",
    ]
    assert unavailable == {}
    assert unavailable_details == {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    assert _resolution.knowledge is not None
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    assert _resolution.knowledge is not None
    assert unavailable == {}
    assert unavailable_details == {}

    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_shared_local_watch_file_event_marks_stale_and_schedules_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem watch events should preserve last-good reads and refresh in the background."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("watch old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    event_delivered = asyncio.Event()

    async def _fake_awatch(
        *_paths: Path,
        stop_event: asyncio.Event,
        **_kwargs: object,
    ) -> AsyncIterator[set[tuple[Change, str]]]:
        yield {(Change.modified, str(doc))}
        event_delivered.set()
        await stop_event.wait()

    monkeypatch.setattr("mindroom.knowledge.watch.awatch", _fake_awatch)
    refresh_scheduler = MagicMock()
    source_watcher = KnowledgeSourceWatcher(refresh_scheduler)

    await source_watcher.sync(config=config, runtime_paths=runtime_paths)
    await asyncio.wait_for(event_delivered.wait(), timeout=1)
    await source_watcher.shutdown()

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    unavailable_details: dict[str, KnowledgeAvailabilityDetail] = {}

    assert state is not None
    assert published_index_refresh_state(state) == "stale"
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable_details.update(_resolution.unavailable)
    assert _resolution.knowledge is not None
    refresh_scheduler.schedule_refresh.assert_called_once()
    assert refresh_scheduler.schedule_refresh.call_args.args == ("docs",)
    assert unavailable_details == {
        "docs": KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.STALE,
            search_available=True,
        ),
    }


@pytest.mark.asyncio
async def test_git_knowledge_polling_schedules_background_refresh_on_startup(tmp_path: Path) -> None:
    """Shared Git bases should schedule their first refresh as soon as runtime support starts."""
    docs_path = tmp_path / "docs"
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", poll_interval_seconds=5)
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
        watch=False,
    )
    runtime_paths = runtime_paths_for(config)
    refresh_scheduler = MagicMock()
    source_watcher = KnowledgeSourceWatcher(refresh_scheduler)

    await source_watcher.sync(config=config, runtime_paths=runtime_paths)
    try:
        for _attempt in range(50):
            if refresh_scheduler.schedule_refresh.called:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("Git poller did not schedule startup refresh")
    finally:
        await source_watcher.shutdown()

    refresh_scheduler.schedule_refresh.assert_called_once()
    assert refresh_scheduler.schedule_refresh.call_args.args == ("docs",)


@pytest.mark.asyncio
async def test_git_knowledge_polling_repeats_after_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared Git bases should keep scheduling refreshes on their configured poll interval."""
    docs_path = tmp_path / "docs"
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", poll_interval_seconds=5)
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
        watch=False,
    )
    runtime_paths = runtime_paths_for(config)
    second_schedule = asyncio.Event()
    refresh_scheduler = MagicMock()

    def _record_schedule(*_args: object, **_kwargs: object) -> None:
        if refresh_scheduler.schedule_refresh.call_count == 2:
            second_schedule.set()

    refresh_scheduler.schedule_refresh.side_effect = _record_schedule
    wait_calls = 0

    async def _fake_wait_for(awaitable: Coroutine[object, object, object], **kwargs: object) -> object:
        nonlocal wait_calls
        assert kwargs == {"timeout": 5.0}
        wait_calls += 1
        if wait_calls == 1:
            awaitable.close()
            raise TimeoutError
        return await awaitable

    monkeypatch.setattr("mindroom.knowledge.watch.asyncio.wait_for", _fake_wait_for)
    source_watcher = KnowledgeSourceWatcher(refresh_scheduler)

    await source_watcher.sync(config=config, runtime_paths=runtime_paths)
    try:
        for _attempt in range(50):
            if second_schedule.is_set():
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("Git poller did not schedule refresh after interval")
    finally:
        await source_watcher.shutdown()

    assert refresh_scheduler.schedule_refresh.call_count == 2
    assert [call.args for call in refresh_scheduler.schedule_refresh.call_args_list] == [("docs",), ("docs",)]


@pytest.mark.asyncio
async def test_schedule_refresh_on_access_reports_stale_while_scheduler_is_active(tmp_path: Path) -> None:
    """Due refresh-on-access remains visible as STALE even when the scheduler already has work active."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("active refresh old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=True)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    unavailable_details: dict[str, KnowledgeAvailabilityDetail] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert [document.content for document in knowledge.search("active", max_results=5)] == ["active refresh old"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    assert unavailable_details == {
        "docs": KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.STALE,
            search_available=True,
        ),
    }
    scheduler.schedule_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_stale_index_metadata_schedules_refresh_without_source_scan(tmp_path: Path) -> None:
    """Ready access only uses persisted metadata/source change markers, not request-time source scans."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("ready index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("ready index changed", encoding="utf-8")
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_stale(key, reason="test_stale")
    clear_published_indexes()
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    second_knowledge = _resolution.knowledge

    assert knowledge is not None
    assert second_knowledge is not None
    assert [document.content for document in knowledge.search("index", max_results=5)] == ["ready index"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    scheduler.schedule_refresh.assert_called_once()
    assert scheduler.schedule_refresh.call_args.args == ("docs",)


@pytest.mark.asyncio
async def test_stale_index_skips_duplicate_refresh_when_scheduler_is_active(tmp_path: Path) -> None:
    """A stale index should not queue another refresh while the scheduler is already active."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("ready index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("ready index changed", encoding="utf-8")
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_stale(key, reason="test_stale")
    clear_published_indexes()
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=True)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert [document.content for document in knowledge.search("index", max_results=5)] == ["ready index"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    scheduler.is_refreshing.assert_called_once()
    scheduler.schedule_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_dashboard_delete_keeps_last_good_best_effort_until_refresh(tmp_path: Path) -> None:
    """A dashboard delete marks stale but old vectors can remain visible until refresh publishes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("deleted secret", encoding="utf-8")
    (docs_path / "keep.md").write_text("kept public", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    initial_knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert initial_knowledge is not None
    assert {document.content for document in initial_knowledge.search("anything", max_results=5)} == {
        "deleted secret",
        "kept public",
    }

    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    try:
        response = TestClient(main.app).delete("/api/knowledge/bases/docs/files/guide.md")
    finally:
        config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = None
    assert response.status_code == 200

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_refresh_failed_preserving_last_good(
        key,
        error="refresh failed after delete",
    )
    stale_knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge

    assert stale_knowledge is not None
    assert {document.content for document in stale_knowledge.search("anything", max_results=5)} == {
        "deleted secret",
        "kept public",
    }
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_dashboard_replacement_upload_keeps_last_good_best_effort_until_refresh(tmp_path: Path) -> None:
    """A replacement upload marks stale but old vectors can remain visible until refresh publishes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("replaced secret", encoding="utf-8")
    (docs_path / "keep.md").write_text("kept public", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    initial_knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert initial_knowledge is not None
    assert {document.content for document in initial_knowledge.search("anything", max_results=5)} == {
        "replaced secret",
        "kept public",
    }

    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    try:
        response = TestClient(main.app).post(
            "/api/knowledge/bases/docs/upload",
            files=[("files", ("guide.md", b"replacement content", "text/markdown"))],
        )
    finally:
        config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = None
    assert response.status_code == 200

    pending_knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert pending_knowledge is not None
    assert {document.content for document in pending_knowledge.search("anything", max_results=5)} == {
        "replaced secret",
        "kept public",
    }

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_refresh_failed_preserving_last_good(
        key,
        error="refresh failed after replacement",
    )
    filtered_knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge

    assert (docs_path / "guide.md").read_text(encoding="utf-8") == "replacement content"
    assert filtered_knowledge is not None
    assert {document.content for document in filtered_knowledge.search("anything", max_results=5)} == {
        "replaced secret",
        "kept public",
    }
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_dashboard_delete_stale_write_failure_keeps_best_effort_source_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale metadata failures schedule refresh instead of stranding source deletes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("restored public", encoding="utf-8")
    config = _config(tmp_path, bases={"research": docs_path, "summary": docs_path}, agent_bases=["research", "summary"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("research", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("summary", config=config, runtime_paths=runtime_paths)
    original_save_stale = knowledge_registry.mark_published_index_stale
    stale_write_count = 0

    def _fail_second_stale_write(*args: object, **kwargs: object) -> None:
        nonlocal stale_write_count
        stale_write_count += 1
        if stale_write_count == 2:
            msg = "same-source stale write failed"
            raise RuntimeError(msg)
        original_save_stale(*args, **kwargs)

    monkeypatch.setattr(knowledge_registry, "mark_published_index_stale", _fail_second_stale_write)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    try:
        with pytest.raises(RuntimeError, match="same-source stale write failed"):
            TestClient(main.app).delete("/api/knowledge/bases/research/files/guide.md")
    finally:
        config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = None

    assert stale_write_count == 2
    assert not (docs_path / "guide.md").exists()
    for base_id in ("research", "summary"):
        lookup = get_published_index(base_id, config=config, runtime_paths=runtime_paths)
        assert lookup.index is not None
        assert [document.content for document in lookup.index.knowledge.search("anything", max_results=5)] == [
            "restored public",
        ]
    assert scheduler.schedule_refresh.call_count == 2
    assert [call.args for call in scheduler.schedule_refresh.call_args_list] == [("research",), ("summary",)]


@pytest.mark.asyncio
async def test_ready_index_access_never_recomputes_source_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """READY request lookup must not walk the corpus to recompute source signatures."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("ready index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    assert state is not None
    assert state.source_signature is not None

    def _unexpected_signature(*_args: object, **_kwargs: object) -> str:
        msg = "READY request lookup must not recompute knowledge source signatures"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.knowledge.manager.knowledge_source_signature", _unexpected_signature)

    assert resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge is not None
    assert resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge is not None


def test_knowledge_file_listing_rejects_symlink_file_escape(tmp_path: Path) -> None:
    """A symlinked file inside the KB must not expose files outside the knowledge root."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    secret = tmp_path / "secret.md"
    secret.write_text("secret outside root", encoding="utf-8")
    try:
        (docs_path / "leak.md").symlink_to(secret)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    assert list_knowledge_files(config, "docs", docs_path) == []


def test_knowledge_file_listing_rejects_symlinked_directory_escape(tmp_path: Path) -> None:
    """Traversal must not follow symlinked directories out of the knowledge root."""
    docs_path = tmp_path / "docs"
    outside = tmp_path / "outside"
    docs_path.mkdir()
    outside.mkdir()
    (outside / "secret.md").write_text("secret through directory", encoding="utf-8")
    try:
        (docs_path / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])

    assert list_knowledge_files(config, "docs", docs_path) == []


@pytest.mark.asyncio
async def test_index_metadata_without_source_signature_is_unavailable_and_schedules_refresh(
    tmp_path: Path,
) -> None:
    """Stale-format published metadata is treated as corrupt instead of interpreted."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stale-format index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = published_index_metadata_path(key)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload.pop("source_signature", None)
    metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    clear_published_indexes()
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_successful_publish_clears_stale_refresh_state(tmp_path: Path) -> None:
    """A successful publish clears stale refresh state."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_stale(key, reason="test_stale")

    stale_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert stale_lookup.index is not None
    assert stale_lookup.availability is KnowledgeAvailability.STALE

    (docs_path / "doc.md").write_text("index updated", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))

    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert state is not None
    assert knowledge_registry.published_index_refresh_state(state) == "none"
    assert state.refresh_job == "idle"
    assert unavailable == {}


@pytest.mark.asyncio
async def test_refreshing_state_cancellation_clears_active_refresh_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during the initial refreshing state write must not leak active status."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    loop = asyncio.get_running_loop()
    refreshing_write_started = asyncio.Event()
    release_refreshing_write = Event()

    def _blocked_refreshing_state(*_args: object, **_kwargs: object) -> None:
        loop.call_soon_threadsafe(refreshing_write_started.set)
        assert release_refreshing_write.wait(timeout=5)

    monkeypatch.setattr(knowledge_refresh_runner, "mark_published_index_refresh_running", _blocked_refreshing_state)

    refresh_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await refreshing_write_started.wait()
    refresh_task.cancel()
    release_refreshing_write.set()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task

    refresh_target = knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths)
    assert knowledge_refresh_runner.is_refresh_active(refresh_target) is False


@pytest.mark.asyncio
async def test_cancelled_refresh_after_refreshing_write_keeps_existing_index_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after the refreshing state write must not clear stale index state."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("stable index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_stale(key, reason="source_changed")

    loop = asyncio.get_running_loop()
    refreshing_saved = asyncio.Event()
    release_refreshing_save = Event()
    original_save_refreshing = knowledge_refresh_runner.mark_published_index_refresh_running

    def _block_after_refreshing_state(*args: object, **kwargs: object) -> None:
        original_save_refreshing(*args, **kwargs)
        loop.call_soon_threadsafe(refreshing_saved.set)
        assert release_refreshing_save.wait(timeout=5)

    monkeypatch.setattr(
        knowledge_refresh_runner,
        "mark_published_index_refresh_running",
        _block_after_refreshing_state,
    )

    refresh_task = asyncio.create_task(
        refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths),
    )
    await refreshing_saved.wait()
    refresh_task.cancel()
    release_refreshing_save.set()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task

    state = load_published_index_state(published_index_metadata_path(key))
    assert knowledge_registry.published_index_refresh_state(state) == "stale"
    assert state is not None
    assert state.refresh_job == "idle"


@pytest.mark.asyncio
async def test_cancelled_refresh_waiting_for_source_lock_clears_running_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation while queued behind another refresh must not leave refresh state running."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("locked refresh", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    loop = asyncio.get_running_loop()
    refreshing_write_count = 0
    second_refreshing_saved = asyncio.Event()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    original_save_refreshing = knowledge_refresh_runner.mark_published_index_refresh_running
    original_reindex = KnowledgeManager.reindex_all

    def _track_refreshing_state(*args: object, **kwargs: object) -> None:
        nonlocal refreshing_write_count
        original_save_refreshing(*args, **kwargs)
        refreshing_write_count += 1
        if refreshing_write_count == 2:
            loop.call_soon_threadsafe(second_refreshing_saved.set)

    async def _blocked_reindex(self: KnowledgeManager) -> int:
        first_entered.set()
        await release_first.wait()
        return await original_reindex(self)

    monkeypatch.setattr(knowledge_refresh_runner, "mark_published_index_refresh_running", _track_refreshing_state)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _blocked_reindex)

    first_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await first_entered.wait()
    second_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await second_refreshing_saved.wait()

    second_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_task

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    assert state is not None
    assert state.refresh_job != "running"

    release_first.set()
    await first_task


@pytest.mark.asyncio
async def test_cancelled_source_lock_waiter_does_not_wedge_later_mutation(tmp_path: Path) -> None:
    """A cancelled queued waiter must not acquire and leak the source lock."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    refresh_target = knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths)
    source_root = knowledge_registry.source_root_for_refresh_target(refresh_target)
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()
    waiter_entered = asyncio.Event()

    async def _hold_lock() -> None:
        async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
            holder_entered.set()
            await release_holder.wait()

    async def _queued_waiter() -> None:
        async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
            waiter_entered.set()

    holder_task = asyncio.create_task(_hold_lock())
    await holder_entered.wait()
    waiter_task = asyncio.create_task(_queued_waiter())
    await _wait_for_refresh_lock_borrowers(source_root, 2)

    waiter_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter_task

    release_holder.set()
    await holder_task

    async with asyncio.timeout(1):
        async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
            pass
    assert not waiter_entered.is_set()


@pytest.mark.asyncio
async def test_refresh_lock_pruning_keeps_queued_waiter_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pruning must not drop a lock entry with an active queued waiter."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    refresh_target = knowledge_registry.resolve_refresh_target("docs", config=config, runtime_paths=runtime_paths)
    source_root = knowledge_registry.source_root_for_refresh_target(refresh_target)
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()
    waiter_entered = asyncio.Event()
    monkeypatch.setattr(knowledge_refresh_runner, "_MAX_REFRESH_LOCKS", 1)

    async def _hold_lock() -> None:
        async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
            holder_entered.set()
            await release_holder.wait()

    async def _queued_waiter() -> None:
        async with knowledge_binding_mutation_lock("docs", config=config, runtime_paths=runtime_paths):
            waiter_entered.set()

    holder_task = asyncio.create_task(_hold_lock())
    await holder_entered.wait()
    waiter_task = asyncio.create_task(_queued_waiter())
    await _wait_for_refresh_lock_borrowers(source_root, 2)
    original_entry = knowledge_refresh_runner._refresh_locks[source_root]

    for index in range(5):
        _create_idle_refresh_lock(
            knowledge_registry.KnowledgeSourceRoot(
                storage_root=str(tmp_path / f"other-{index}"),
                knowledge_path=str(tmp_path / f"other-{index}" / "docs"),
            ),
        )

    assert knowledge_refresh_runner._refresh_locks.get(source_root) is original_entry

    release_holder.set()
    async with asyncio.timeout(1):
        await asyncio.gather(holder_task, waiter_task)
    assert waiter_entered.is_set()


def test_source_changed_updates_refresh_state_without_changing_index(tmp_path: Path) -> None:
    """Source mutation records source changes without mutating published index data."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("published old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    default_collection = manager._default_collection_name()
    _VectorDb.collections[default_collection] = [
        {"content": "published old", "metadata": {"source_path": "guide.md"}},
    ]
    metadata_path = published_index_metadata_path(key)
    knowledge_registry.save_published_index_state(
        metadata_path,
        knowledge_registry.PublishedIndexState(
            settings=key.indexing_settings,
            status="complete",
            collection=default_collection,
            indexed_count=1,
            source_signature="test-source-signature",
        ),
    )
    knowledge_registry.mark_published_index_refresh_succeeded(key)

    marked_base_ids = knowledge_registry.mark_knowledge_source_changed(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
    )
    state = load_published_index_state(metadata_path)

    assert marked_base_ids == ("docs",)
    assert _VectorDb.collections[default_collection] == [
        {"content": "published old", "metadata": {"source_path": "guide.md"}},
    ]
    assert state is not None
    assert knowledge_registry.published_index_refresh_state(state) == "stale"
    assert state.refresh_job == "pending"


@pytest.mark.asyncio
async def test_mark_stale_fans_out_to_duplicate_physical_sources(tmp_path: Path) -> None:
    """Mutating one base should stale every published index that reads the same source folder."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("shared source old", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)
    assert beta_lookup.index is not None
    assert beta_lookup.availability is KnowledgeAvailability.READY
    doc.write_text("shared source new", encoding="utf-8")

    marked_base_ids = knowledge_registry.mark_knowledge_source_changed(
        "alpha",
        config=config,
        runtime_paths=runtime_paths,
    )
    beta_key = resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)
    beta_state = load_published_index_state(published_index_metadata_path(beta_key))
    refreshed_beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)

    assert marked_base_ids == ("alpha", "beta")
    assert beta_state is not None
    assert knowledge_registry.published_index_refresh_state(beta_state) == "stale"
    assert refreshed_beta_lookup.availability is KnowledgeAvailability.STALE


@pytest.mark.asyncio
async def test_async_source_changed_cancellation_waits_for_state_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during stale writes must wait for the metadata commit before propagating."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("cached old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    ready_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert ready_lookup.index is not None
    assert ready_lookup.availability is KnowledgeAvailability.READY

    loop = asyncio.get_running_loop()
    stale_written = asyncio.Event()
    release_stale_write = Event()
    original_mark = knowledge_registry._mark_published_index_key_stale_on_disk

    def _block_after_stale_write(matching_key: knowledge_registry.PublishedIndexKey, *, reason: str) -> bool:
        result = original_mark(matching_key, reason=reason)
        loop.call_soon_threadsafe(stale_written.set)
        assert release_stale_write.wait(timeout=5)
        return result

    monkeypatch.setattr(knowledge_registry, "_mark_published_index_key_stale_on_disk", _block_after_stale_write)

    mark_task = asyncio.create_task(
        knowledge_registry.mark_knowledge_source_changed_async(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        ),
    )
    await stale_written.wait()
    mark_task.cancel()
    release_stale_write.set()
    with pytest.raises(asyncio.CancelledError):
        await mark_task

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    refreshed_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert state is not None
    assert knowledge_registry.published_index_refresh_state(state) == "stale"
    assert refreshed_lookup.availability is KnowledgeAvailability.STALE


@pytest.mark.asyncio
async def test_async_source_changed_cancellation_finishes_same_source_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after one alias write must still mark every same-source alias stale."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("shared cached old", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    doc.write_text("shared cached new", encoding="utf-8")

    loop = asyncio.get_running_loop()
    first_alias_written = asyncio.Event()
    release_remaining_writes = Event()
    original_mark = knowledge_registry._mark_published_index_key_stale_on_disk
    written_base_ids: list[str] = []

    def _block_after_first_alias(matching_key: knowledge_registry.PublishedIndexKey, *, reason: str) -> bool:
        result = original_mark(matching_key, reason=reason)
        written_base_ids.append(matching_key.base_id)
        if len(written_base_ids) == 1:
            loop.call_soon_threadsafe(first_alias_written.set)
            assert release_remaining_writes.wait(timeout=5)
        return result

    monkeypatch.setattr(knowledge_registry, "_mark_published_index_key_stale_on_disk", _block_after_first_alias)

    mark_task = asyncio.create_task(
        knowledge_registry.mark_knowledge_source_changed_async(
            "alpha",
            config=config,
            runtime_paths=runtime_paths,
        ),
    )
    await first_alias_written.wait()
    mark_task.cancel()
    release_remaining_writes.set()
    with pytest.raises(asyncio.CancelledError):
        await mark_task

    alpha_key = resolve_published_index_key("alpha", config=config, runtime_paths=runtime_paths)
    beta_key = resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)
    alpha_state = load_published_index_state(published_index_metadata_path(alpha_key))
    beta_state = load_published_index_state(published_index_metadata_path(beta_key))

    assert tuple(written_base_ids) == ("alpha", "beta")
    assert knowledge_registry.published_index_refresh_state(alpha_state) == "stale"
    assert knowledge_registry.published_index_refresh_state(beta_state) == "stale"


@pytest.mark.asyncio
async def test_async_source_changed_recached_index_reports_refresh_state_after_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readers may keep last-good handles while refresh state changes to stale after commit."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("recache old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    ready_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert ready_lookup.index is not None
    assert ready_lookup.availability is KnowledgeAvailability.READY

    loop = asyncio.get_running_loop()
    stale_write_started = asyncio.Event()
    release_stale_write = Event()
    original_mark = knowledge_registry._mark_published_index_key_stale_on_disk

    def _block_before_stale_write(matching_key: knowledge_registry.PublishedIndexKey, *, reason: str) -> bool:
        loop.call_soon_threadsafe(stale_write_started.set)
        assert release_stale_write.wait(timeout=5)
        return original_mark(matching_key, reason=reason)

    monkeypatch.setattr(knowledge_registry, "_mark_published_index_key_stale_on_disk", _block_before_stale_write)

    mark_task = asyncio.create_task(
        knowledge_registry.mark_knowledge_source_changed_async(
            "docs",
            config=config,
            runtime_paths=runtime_paths,
        ),
    )
    await stale_write_started.wait()
    recached_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert recached_lookup.index is not None
    assert recached_lookup.availability is KnowledgeAvailability.READY

    release_stale_write.set()
    assert await mark_task == ("docs",)
    final_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert final_lookup.availability is KnowledgeAvailability.STALE


@pytest.mark.asyncio
async def test_local_refresh_marks_duplicate_source_sibling_stale_after_source_change(tmp_path: Path) -> None:
    """Refreshing one local alias after an external source edit should stale sibling aliases."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("shared local old", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)
    assert beta_lookup.index is not None
    assert beta_lookup.availability is KnowledgeAvailability.READY

    doc.write_text("shared local new", encoding="utf-8")
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    alpha_lookup = get_published_index("alpha", config=config, runtime_paths=runtime_paths)
    beta_key = resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)
    beta_state = load_published_index_state(published_index_metadata_path(beta_key))
    refreshed_beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)

    assert alpha_lookup.index is not None
    assert [document.content for document in alpha_lookup.index.knowledge.search("local", max_results=5)] == [
        "shared local new",
    ]
    assert beta_state is not None
    assert knowledge_registry.published_index_refresh_state(beta_state) == "stale"
    assert refreshed_beta_lookup.index is not None
    assert refreshed_beta_lookup.availability is KnowledgeAvailability.STALE
    assert [document.content for document in refreshed_beta_lookup.index.knowledge.search("local", max_results=5)] == [
        "shared local old",
    ]


def test_config_rejects_parent_child_knowledge_roots(tmp_path: Path) -> None:
    """Configured local knowledge roots may be exact aliases, but not overlapping subtrees."""
    parent = tmp_path / "docs"
    child = parent / "nested"

    with pytest.raises(ValueError, match="knowledge_bases paths must not overlap"):
        _config(
            tmp_path,
            bases={"parent": parent, "child": child},
            agent_bases=["parent"],
        )


def test_config_rejects_exact_duplicate_roots_with_mixed_git_ownership(tmp_path: Path) -> None:
    """Exact duplicate knowledge roots must agree on local vs Git source ownership."""
    docs = tmp_path / "docs"

    with pytest.raises(ValueError, match="exact duplicate aliases must use compatible source configuration"):
        _config(
            tmp_path,
            bases={"local": docs, "git": docs},
            agent_bases=["local"],
            git_configs={"git": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git")},
        )


def test_config_rejects_exact_duplicate_git_roots_with_different_source_semantics(tmp_path: Path) -> None:
    """Exact duplicate Git roots must not share one checkout across incompatible source config."""
    docs = tmp_path / "docs"

    with pytest.raises(ValueError, match="exact duplicate aliases must use compatible source configuration"):
        _config(
            tmp_path,
            bases={"main": docs, "release": docs},
            agent_bases=["main"],
            git_configs={
                "main": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main"),
                "release": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="release"),
            },
        )


def test_config_rejects_exact_duplicate_git_roots_with_different_passwordless_ssh_usernames(
    tmp_path: Path,
) -> None:
    """Passwordless SSH usernames are part of duplicate-root Git source identity."""
    docs = tmp_path / "docs"

    with pytest.raises(ValueError, match="exact duplicate aliases must use compatible source configuration"):
        _config(
            tmp_path,
            bases={"git_user": docs, "deploy_user": docs},
            agent_bases=["git_user"],
            git_configs={
                "git_user": KnowledgeGitConfig(repo_url="ssh://git@example.com/org/repo.git"),
                "deploy_user": KnowledgeGitConfig(repo_url="ssh://deploy@example.com/org/repo.git"),
            },
        )


def test_config_allows_exact_duplicate_roots_with_compatible_source_semantics(tmp_path: Path) -> None:
    """Exact duplicate aliases remain valid when their source ownership semantics match."""
    docs = tmp_path / "docs"
    git_config = KnowledgeGitConfig(
        repo_url="https://token:secret@example.com/org/repo.git?token=query-secret#fragment-secret",
        branch="main",
        include_patterns=["docs/**"],
        exclude_patterns=["docs/private/**"],
    )

    config = _config(
        tmp_path,
        bases={"alpha": docs, "beta": docs},
        agent_bases=["alpha", "beta"],
        git_configs={
            "alpha": git_config,
            "beta": git_config.model_copy(deep=True),
        },
    )

    assert sorted(config.knowledge_bases) == ["alpha", "beta"]


def test_config_allows_exact_duplicate_git_roots_with_different_filters(tmp_path: Path) -> None:
    """One Git checkout may back multiple filtered knowledge views."""
    docs = tmp_path / "docs"

    config = _config(
        tmp_path,
        bases={"docs": docs, "source": docs},
        agent_bases=["docs", "source"],
        git_configs={
            "docs": KnowledgeGitConfig(
                repo_url="https://example.com/org/repo.git",
                branch="main",
                include_patterns=["docs/**"],
            ),
            "source": KnowledgeGitConfig(
                repo_url="https://example.com/org/repo.git",
                branch="main",
                include_patterns=["src/**"],
            ),
        },
    )

    assert sorted(config.knowledge_bases) == ["docs", "source"]


def test_config_allows_exact_duplicate_local_roots_with_different_filters(tmp_path: Path) -> None:
    """One local folder may back multiple filtered knowledge views."""
    docs = tmp_path / "docs"
    runtime_paths = test_runtime_paths(tmp_path)

    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    knowledge_bases=["markdown", "python"],
                ),
            },
            models={},
            knowledge_bases={
                "markdown": KnowledgeBaseConfig(path=str(docs), include_extensions=[".md"]),
                "python": KnowledgeBaseConfig(path=str(docs), include_extensions=[".py"]),
            },
        ),
        runtime_paths,
    )

    assert sorted(config.knowledge_bases) == ["markdown", "python"]


def test_raw_git_url_index_metadata_is_config_mismatch(tmp_path: Path) -> None:
    """Raw Git URLs in persisted settings are stale-format metadata, not a compatible identity."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("raw git metadata", encoding="utf-8")
    raw_repo_url = "https://token:secret@example.com/org/repo.git"
    git_config = KnowledgeGitConfig(repo_url=raw_repo_url)
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    stale_settings = list(key.indexing_settings)
    stale_settings[knowledge_manager_module.INDEXING_SETTINGS_REPO_IDENTITY_INDEX] = raw_repo_url
    collection = "raw_git_metadata_collection"
    _VectorDb.collections[collection] = [
        {"content": "raw git metadata", "metadata": {"source_path": "doc.md"}},
    ]
    metadata_path = published_index_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "settings": stale_settings,
                "status": "complete",
                "collection": collection,
                "indexed_count": 1,
                "source_signature": "test-source-signature",
            },
        ),
        encoding="utf-8",
    )
    knowledge_registry.mark_published_index_refresh_succeeded(key)

    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert lookup.index is None
    assert lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH


def test_passwordless_ssh_username_change_invalidates_published_index(tmp_path: Path) -> None:
    """Passwordless SSH usernames are part of the persisted index identity."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git user index", encoding="utf-8")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="ssh://git@example.com/org/repo.git")},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    collection = "ssh_git_user_collection"
    _VectorDb.collections[collection] = [
        {"content": "git user index", "metadata": {"source_path": "doc.md"}},
    ]
    metadata_path = published_index_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "settings": list(key.indexing_settings),
                "status": "complete",
                "collection": collection,
                "indexed_count": 1,
                "source_signature": "test-source-signature",
            },
        ),
        encoding="utf-8",
    )
    knowledge_registry.mark_published_index_refresh_succeeded(key)
    changed_config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="ssh://deploy@example.com/org/repo.git")},
    )
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    lookup = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)
    _resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert lookup.index is None
    assert lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH
    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    scheduler.schedule_refresh.assert_called_once()


def test_metadata_state_alone_serves_published_index(tmp_path: Path) -> None:
    """The simplified metadata model keeps active state in the metadata file."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("metadata index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    collection = "metadata_collection"
    _VectorDb.collections[collection] = [
        {"content": "metadata index", "metadata": {"source_path": "doc.md"}},
    ]
    metadata_path = published_index_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "settings": list(key.indexing_settings),
                "status": "complete",
                "collection": collection,
                "indexed_count": 1,
                "source_signature": "test-source-signature",
            },
        ),
        encoding="utf-8",
    )
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert _refresh_state_for_key(key) == "none"
    assert lookup.index is not None
    assert lookup.availability is KnowledgeAvailability.READY


def test_indexing_settings_layout_constants_match_settings_key(tmp_path: Path) -> None:
    """Compatibility helpers must stay aligned with the indexing settings tuple layout."""
    docs_path = tmp_path / "docs"
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url="https://example.com/org/repo.git")},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)

    assert len(key.indexing_settings) == knowledge_manager_module.INDEXING_SETTINGS_LAYOUT_LENGTH
    assert key.indexing_settings[knowledge_manager_module.INDEXING_SETTINGS_BASE_ID_INDEX] == "docs"
    assert key.indexing_settings[knowledge_manager_module.INDEXING_SETTINGS_CHUNK_SIZE_INDEX] == "5000"
    assert key.indexing_settings[knowledge_manager_module.INDEXING_SETTINGS_CHUNK_OVERLAP_INDEX] == "0"
    assert key.indexing_settings[knowledge_manager_module.INDEXING_SETTINGS_REPO_IDENTITY_INDEX] == (
        credential_free_url_identity("https://example.com/org/repo.git")
    )
    assert (
        knowledge_manager_module.INDEXING_SETTINGS_REPO_IDENTITY_INDEX
        in knowledge_manager_module.INDEXING_SETTINGS_CORPUS_COMPATIBLE_INDEXES
    )


@pytest.mark.asyncio
async def test_git_ready_index_schedules_refresh_after_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale Git access can schedule refresh without scanning the local checkout."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git index", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", poll_interval_seconds=5)
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_success(self: KnowledgeManager) -> dict[str, object]:
        self._git_last_successful_commit = "rev-a"
        _set_git_tracked_files(self, "doc.md")
        return {"updated": False, "changed_count": 0, "removed_count": 0}

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync_success)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = published_index_metadata_path(key)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["last_published_at"] = "2000-01-01T00:00:00+00:00"
    payload["last_refresh_at"] = "2000-01-01T00:00:00+00:00"
    metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    clear_published_indexes()
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    def _unexpected_signature(*_args: object, **_kwargs: object) -> str:
        msg = "git ready access should not scan the local corpus"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.knowledge.manager.knowledge_source_signature", _unexpected_signature)
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert [document.content for document in knowledge.search("git", max_results=5)] == ["git index"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_private_git_schedule_refresh_on_access_honors_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requester-local Git knowledge should not poll before its configured interval has elapsed."""
    runtime_paths = test_runtime_paths(tmp_path)
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", poll_interval_seconds=60)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge", git=git_config),
                    ),
                ),
            },
            models={},
        ),
        runtime_paths,
    )
    base_id = config.get_agent_private_knowledge_base_id("helper")
    assert base_id is not None
    identity = _identity("@alice:localhost")
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
        create=True,
    )
    knowledge_path = Path(key.knowledge_path)
    knowledge_path.mkdir(parents=True, exist_ok=True)
    (knowledge_path / "note.md").write_text("alice private git note", encoding="utf-8")

    async def _sync_success(self: KnowledgeManager) -> dict[str, object]:
        self._git_last_successful_commit = "rev-a"
        _set_git_tracked_files(self, "note.md")
        return {"updated": False, "changed_count": 0, "removed_count": 0}

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync_success)
    await refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert unavailable == {}
    scheduler.schedule_refresh.assert_not_called()

    metadata_path = published_index_metadata_path(key)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["last_published_at"] = "2000-01-01T00:00:00+00:00"
    payload["last_refresh_at"] = "2000-01-01T00:00:00+00:00"
    metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    clear_published_indexes()
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    stale_knowledge = _resolution.knowledge

    assert stale_knowledge is not None
    assert unavailable == {base_id: KnowledgeAvailability.STALE}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_private_git_updated_refresh_preserves_execution_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private Git updates must mark stale and rebuild through the requester binding."""
    runtime_paths = test_runtime_paths(tmp_path)
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge", git=git_config),
                    ),
                ),
            },
            models={},
        ),
        runtime_paths,
    )
    base_id = config.get_agent_private_knowledge_base_id("helper")
    assert base_id is not None
    identity = _identity("@alice:localhost")
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
        create=True,
    )
    knowledge_path = Path(key.knowledge_path)
    knowledge_path.mkdir(parents=True, exist_ok=True)
    (knowledge_path / "note.md").write_text("alice private git updated", encoding="utf-8")

    async def _sync_updated(self: KnowledgeManager) -> dict[str, object]:
        self._git_last_successful_commit = "rev-private"
        _set_git_tracked_files(self, "note.md")
        return {"updated": True, "changed_count": 1, "removed_count": 0}

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync_updated)

    result = await refresh_knowledge_binding(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
    )
    lookup = get_published_index(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity)

    assert result.index_published is True
    assert lookup.index is not None
    assert lookup.availability is KnowledgeAvailability.READY
    assert [document.content for document in lookup.index.knowledge.search("updated", max_results=5)] == [
        "alice private git updated",
    ]


@pytest.mark.asyncio
async def test_git_source_sync_does_not_mutate_index_directly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git source sync should never bypass candidate publish by mutating the live index."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    manager = KnowledgeManager("docs", config, runtime_paths)

    async def _sync_once(_git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        return {"changed.md"}, {"removed.md"}, True

    async def _git_rev_parse(_ref: str) -> str:
        return "rev-source-only"

    async def _git_checkout_present() -> bool:
        return True

    monkeypatch.setattr(manager, "_sync_git_source_once", _sync_once)
    monkeypatch.setattr(manager, "_git_rev_parse", _git_rev_parse)
    monkeypatch.setattr(manager, "_git_checkout_present", _git_checkout_present)

    result = await manager.sync_git_source()

    assert not hasattr(manager, "remove_file")
    assert not hasattr(manager, "index_file")
    assert result == {"updated": True, "changed_count": 1, "removed_count": 1}
    assert manager._git_last_successful_commit == "rev-source-only"


@pytest.mark.asyncio
async def test_existing_published_index_is_used_while_refresh_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow refresh builds a candidate while readers continue using the last-good index."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / "doc.md").write_text("new index", encoding="utf-8")

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
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
    ) -> bool:
        if knowledge is not None and knowledge is not self._knowledge and not started.is_set():
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

    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert knowledge is not None
    assert [document.content for document in knowledge.search("index", max_results=5)] == ["old index"]

    release.set()
    await refresh_task
    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert knowledge is not None
    assert [document.content for document in knowledge.search("index", max_results=5)] == ["new index"]


@pytest.mark.asyncio
async def test_cancelled_refresh_deletes_unpublished_candidate_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a candidate refresh must not leave an owned candidate collection behind."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("cancel stable", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    existing_collections = set(_VectorDb.collections)
    doc.write_text("cancel candidate", encoding="utf-8")
    candidate_started = asyncio.Event()
    original_index_file_locked = KnowledgeManager._index_file_locked

    async def _block_candidate(
        self: KnowledgeManager,
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
    ) -> bool:
        if knowledge is not None and knowledge is not self._knowledge:
            candidate_started.set()
            await asyncio.Event().wait()
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
    await candidate_started.wait()
    cancelled_candidate_collections = set(_VectorDb.collections) - existing_collections
    assert any("_candidate_" in collection for collection in cancelled_candidate_collections)

    refresh_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task

    assert set(_VectorDb.collections).isdisjoint(cancelled_candidate_collections)
    knowledge = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert knowledge is not None
    assert [document.content for document in knowledge.search("cancel", max_results=5)] == ["cancel stable"]


@pytest.mark.asyncio
async def test_cancelled_publish_metadata_save_keeps_published_candidate_collection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling during READY metadata save must not delete the metadata's candidate collection."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("stable metadata", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    cached_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert cached_lookup.index is not None
    assert [document.content for document in cached_lookup.index.knowledge.search("metadata", max_results=5)] == [
        "stable metadata",
    ]
    doc.write_text("candidate metadata", encoding="utf-8")
    loop = asyncio.get_running_loop()
    metadata_saved = asyncio.Event()
    release_metadata_save = Event()
    original_save = KnowledgeManager._save_persisted_index_state

    def _block_after_candidate_metadata_save(
        self: KnowledgeManager,
        status: object,
        **kwargs: object,
    ) -> None:
        original_save(self, status, **kwargs)
        if status == "complete" and "_candidate_" in str(kwargs.get("collection")):
            loop.call_soon_threadsafe(metadata_saved.set)
            assert release_metadata_save.wait(timeout=5)

    monkeypatch.setattr(KnowledgeManager, "_save_persisted_index_state", _block_after_candidate_metadata_save)

    refresh_task = asyncio.create_task(refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths))
    await metadata_saved.wait()
    refresh_task.cancel()
    await asyncio.sleep(0)
    release_metadata_save.set()
    with pytest.raises(asyncio.CancelledError):
        await refresh_task

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    assert state is not None
    assert state.collection is not None
    assert "_candidate_" in state.collection
    assert state.collection in _VectorDb.collections
    assert knowledge_registry.published_index_refresh_state(state) == "none"
    assert state.refresh_job == "idle"

    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("metadata", max_results=5)] == [
        "candidate metadata",
    ]


@pytest.mark.asyncio
async def test_refresh_discards_candidate_when_sources_change_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published metadata stays bound to the exact corpus that was indexed."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("stable index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("candidate index", encoding="utf-8")
    original_reindex_files_locked = KnowledgeManager._reindex_files_locked

    async def _mutate_after_candidate_index(
        self: KnowledgeManager,
        files: list[Path],
        *,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
    ) -> int:
        indexed_count = await original_reindex_files_locked(
            self,
            files,
            knowledge=knowledge,
            indexed_files=indexed_files,
            indexed_signatures=indexed_signatures,
        )
        (docs_path / "late.md").write_text("late addition", encoding="utf-8")
        return indexed_count

    monkeypatch.setattr(KnowledgeManager, "_reindex_files_locked", _mutate_after_candidate_index)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is False
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert result.last_error == "Knowledge source changed during refresh; refresh skipped"
    assert lookup.index is not None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    assert [document.content for document in lookup.index.knowledge.search("index", max_results=5)] == [
        "stable index",
    ]


@pytest.mark.asyncio
async def test_same_physical_binding_refreshes_are_serialized_across_config_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh writes are serialized by physical storage target, not settings-sensitive index key."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("index", encoding="utf-8")
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
async def test_shared_source_mutation_waits_for_duplicate_base_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate bases sharing one source folder must serialize refreshes and source mutations."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("index", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    refresh_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    mutation_entered = asyncio.Event()

    async def _blocked_reindex(self: KnowledgeManager) -> int:
        _ = self
        refresh_entered.set()
        await release_refresh.wait()
        return 0

    async def _mutate_shared_source() -> None:
        async with knowledge_binding_mutation_lock("beta", config=config, runtime_paths=runtime_paths):
            mutation_entered.set()
            doc.write_text("mutated", encoding="utf-8")
            knowledge_registry.mark_knowledge_source_changed(
                "beta",
                config=config,
                runtime_paths=runtime_paths,
            )

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _blocked_reindex)

    refresh_task = asyncio.create_task(refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths))
    await refresh_entered.wait()
    mutation_task = asyncio.create_task(_mutate_shared_source())
    await asyncio.sleep(0)

    assert not mutation_entered.is_set()

    release_refresh.set()
    await asyncio.gather(refresh_task, mutation_task)

    assert mutation_entered.is_set()
    assert doc.read_text(encoding="utf-8") == "mutated"


@pytest.mark.asyncio
async def test_refresh_generations_keep_latest_index_without_protecting_old_handles(tmp_path: Path) -> None:
    """Old read handles are best effort; refresh cleanup only guarantees the next active index."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("generation one", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    first_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert first_lookup.index is not None
    first_collection = first_lookup.index.knowledge.vector_db.collection_name

    for generation in range(2, 7):
        doc.write_text(f"generation {generation}", encoding="utf-8")
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert first_collection not in _VectorDb.collections
    latest = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert latest.index is not None
    assert [document.content for document in latest.index.knowledge.search("generation", max_results=5)] == [
        "generation 6",
    ]


@pytest.mark.asyncio
async def test_publish_invalidates_cached_indexes_for_same_physical_binding(tmp_path: Path) -> None:
    """A config transition and revert must not resurrect an older cached handle for the same path."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("config a", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    cached_a = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert cached_a.index is not None
    assert [document.content for document in cached_a.index.knowledge.search("config", max_results=5)] == [
        "config a",
    ]

    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    doc.write_text("config b", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)
    reverted_lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert reverted_lookup.index is not None
    assert reverted_lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH
    assert [document.content for document in reverted_lookup.index.knowledge.search("config", max_results=5)] == [
        "config b",
    ]


@pytest.mark.asyncio
async def test_successful_refreshes_keep_only_published_index(tmp_path: Path) -> None:
    """Repeated publishes keep the published index and clean older generations best effort."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("generation 0", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    for generation in range(6):
        doc.write_text(f"generation {generation}", encoding="utf-8")
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    assert state is not None
    assert state.collection in _VectorDb.collections
    assert len(_VectorDb.collections) == 1
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("generation", max_results=5)] == [
        "generation 5",
    ]


@pytest.mark.asyncio
async def test_refresh_rebuilds_stale_list_metadata_without_serving_old_collection(tmp_path: Path) -> None:
    """List-shaped metadata is stale format and forces a fresh publish."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("stale list old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    default_collection = manager._default_collection_name()
    _VectorDb.collections[default_collection] = [
        {"content": "stale list old", "metadata": {"source_path": "doc.md"}},
    ]
    metadata_path = published_index_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(list(key.indexing_settings)), encoding="utf-8")
    doc.write_text("stale list new", encoding="utf-8")

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert default_collection not in _VectorDb.collections
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("stale", max_results=5)] == [
        "stale list new",
    ]


@pytest.mark.asyncio
async def test_superseded_collection_listing_failure_is_best_effort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup listing failures must not turn an already-committed publish into a refresh failure."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("cleanup old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("cleanup new", encoding="utf-8")

    def _raise_list_collections(self: _Client) -> list[str]:
        _ = self
        msg = "list failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(_Client, "list_collections", _raise_list_collections)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.availability is KnowledgeAvailability.READY
    assert lookup.index is not None
    assert lookup.availability is KnowledgeAvailability.READY
    assert [document.content for document in lookup.index.knowledge.search("cleanup", max_results=5)] == [
        "cleanup new",
    ]


@pytest.mark.asyncio
async def test_failed_refresh_preserves_last_good_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed candidate build marks stale availability but keeps serving the old collection."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stable index", encoding="utf-8")
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
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
    ) -> bool:
        if knowledge is not None and knowledge is not self._knowledge:
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
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert knowledge is not None
    assert [document.content for document in knowledge.search("index", max_results=5)] == ["stable index"]


@pytest.mark.asyncio
async def test_metadata_save_failure_after_candidate_index_keeps_serving_last_good(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate whose metadata did not commit must not replace the published read handle."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("stable metadata index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("uncommitted candidate index", encoding="utf-8")
    original_save = KnowledgeManager._save_persisted_index_state

    def _fail_candidate_metadata_save(
        self: KnowledgeManager,
        status: object,
        **kwargs: object,
    ) -> None:
        if status == "complete" and "_candidate_" in str(kwargs.get("collection")):
            msg = "metadata commit failed"
            raise OSError(msg)
        original_save(self, status, **kwargs)

    monkeypatch.setattr(KnowledgeManager, "_save_persisted_index_state", _fail_candidate_metadata_save)
    with pytest.raises(OSError, match="metadata commit failed"):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert knowledge is not None
    assert [document.content for document in knowledge.search("index", max_results=5)] == [
        "stable metadata index",
    ]


@pytest.mark.asyncio
async def test_partial_refresh_after_cached_index_updates_failed_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial refresh must not leave the process-local READY index hiding failure metadata."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "good.md").write_text("last good", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge is not None
    (docs_path / "bad.md").write_text("bad candidate", encoding="utf-8")
    original_index_file_locked = KnowledgeManager._index_file_locked

    async def _skip_bad_file(
        self: KnowledgeManager,
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: object | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
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
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert result.index_published is False
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert knowledge is not None
    assert [document.content for document in knowledge.search("good", max_results=5)] == ["last good"]


@pytest.mark.asyncio
async def test_embedder_config_mismatch_returns_no_incompatible_index(tmp_path: Path) -> None:
    """An embedder-changing config mismatch should not query old vectors with the new embedder."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old embedder index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    changed_config = config.model_copy(deep=True)
    changed_config.memory.embedder.config.model = "text-embedding-3-large"
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_config_mismatch_refresh_cooldown_is_settings_aware(tmp_path: Path) -> None:
    """A newer config mismatch for the same binding must not be dropped by the request cooldown."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()

    assert (
        resolve_agent_knowledge_access("helper", changed_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is not None
    )
    assert (
        resolve_agent_knowledge_access("helper", newer_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is not None
    )

    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


@pytest.mark.asyncio
async def test_initializing_refresh_cooldown_is_settings_aware(tmp_path: Path) -> None:
    """A cold initial load under old settings must not suppress a newer config's initial load."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()

    assert (
        resolve_agent_knowledge_access("helper", changed_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is None
    )
    assert (
        resolve_agent_knowledge_access("helper", newer_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is None
    )

    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


@pytest.mark.asyncio
async def test_cold_failed_refresh_cooldown_is_settings_aware(tmp_path: Path) -> None:
    """A failed cold refresh under old settings must not suppress a newer config's retry."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_refresh_failed_preserving_last_good(key, error="cold failure")
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    _resolution = resolve_agent_knowledge_access(
        "helper",
        newer_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})

    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


@pytest.mark.asyncio
async def test_failed_git_refresh_cooldown_is_credentials_service_aware(tmp_path: Path) -> None:
    """Changing Git auth service config should bypass the failed-refresh retry cooldown."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    git_config = KnowledgeGitConfig(
        repo_url="https://example.com/org/private.git",
        credentials_service="old_service",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_refresh_failed_preserving_last_good(key, error="auth failed")
    changed_config = config.model_copy(deep=True)
    changed_git_config = changed_config.knowledge_bases["docs"].git
    assert changed_git_config is not None
    changed_git_config.credentials_service = "new_service"
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()

    assert (
        resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler).knowledge is None
    )
    assert (
        resolve_agent_knowledge_access("helper", changed_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is None
    )

    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is changed_config


@pytest.mark.asyncio
async def test_failed_git_refresh_cooldown_is_embedded_userinfo_aware(tmp_path: Path) -> None:
    """Changing embedded Git URL auth should bypass cooldown without storing the secret."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    git_config = KnowledgeGitConfig(
        repo_url="https://git-user:old-secret@example.com/org/private.git",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge_registry.mark_published_index_refresh_failed_preserving_last_good(key, error="auth failed")
    changed_config = config.model_copy(deep=True)
    changed_git_config = changed_config.knowledge_bases["docs"].git
    assert changed_git_config is not None
    changed_git_config.repo_url = "https://git-user:new-secret@example.com/org/private.git"
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()

    assert (
        resolve_agent_knowledge_access("helper", config, runtime_paths, refresh_scheduler=scheduler).knowledge is None
    )
    assert (
        resolve_agent_knowledge_access("helper", changed_config, runtime_paths, refresh_scheduler=scheduler).knowledge
        is None
    )

    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is changed_config
    cooldown_keys = repr(tuple(knowledge_utils._refresh_scheduled_at))
    assert "old-secret" not in cooldown_keys
    assert "new-secret" not in cooldown_keys


@pytest.mark.asyncio
@pytest.mark.parametrize("availability", [KnowledgeAvailability.STALE, KnowledgeAvailability.REFRESH_FAILED])
async def test_stale_or_failed_index_reports_chunking_config_mismatch_before_cooldown(
    tmp_path: Path,
    availability: KnowledgeAvailability,
) -> None:
    """Stale/failed metadata must not suppress refreshes for newer chunking settings."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    if availability is KnowledgeAvailability.STALE:
        knowledge_registry.mark_published_index_stale(key, reason="test_stale")
    else:
        knowledge_registry.mark_published_index_refresh_failed_preserving_last_good(key, error="previous failure")
    clear_published_indexes()
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    assert _resolution.knowledge is not None
    _resolution = resolve_agent_knowledge_access(
        "helper",
        newer_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    assert _resolution.knowledge is not None

    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    assert scheduler.schedule_refresh.call_count == 2
    assert scheduler.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert scheduler.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


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
async def test_corpus_changing_config_mismatch_returns_no_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: object,
) -> None:
    """Source identity and membership filter changes must not serve old content."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old corpus index", encoding="utf-8")
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

    async def _sync_success(self: KnowledgeManager) -> dict[str, object]:
        self._git_last_successful_commit = "rev-a"
        _set_git_tracked_files(self, "doc.md")
        return {"updated": True, "changed_count": 1, "removed_count": 0}

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync_success)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    changed_config = config.model_copy(deep=True)
    mutate(changed_config)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        changed_config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_failed_refresh_after_config_change_preserves_published_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed candidate refresh must not rewrite last-good metadata to the attempted settings."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stable index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    old_key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    old_state = load_published_index_state(published_index_metadata_path(old_key))
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
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
    ) -> bool:
        _ = (self, resolved_path, upsert, knowledge, indexed_files, indexed_signatures)
        msg = "candidate failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _fail_candidate)
    with pytest.raises(RuntimeError, match="candidate failed"):
        await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)

    changed_key = resolve_published_index_key("docs", config=changed_config, runtime_paths=runtime_paths)
    preserved_state = load_published_index_state(published_index_metadata_path(changed_key))
    assert preserved_state is not None
    assert preserved_state.settings == old_state.settings
    assert preserved_state.collection == old_state.collection
    assert knowledge_registry.published_index_refresh_state(preserved_state) == "refresh_failed"
    assert preserved_state.last_error == "candidate failed"

    lookup = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH


def test_stale_metadata_without_collection_returns_unavailable_index(tmp_path: Path) -> None:
    """Metadata alone must not create or expose an empty ready collection."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = published_index_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "settings": list(key.indexing_settings),
                "status": "complete",
                "collection": "missing_collection",
                "indexed_count": 1,
                "source_signature": "test-source-signature",
            },
        ),
        encoding="utf-8",
    )
    knowledge_registry.mark_published_index_refresh_succeeded(key)

    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert lookup.index is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    assert "missing_collection" not in _VectorDb.collections


def test_lookup_failure_after_binding_resolution_schedules_repair_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved binding with a broken read handle should still queue a repair refresh."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = published_index_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "settings": list(key.indexing_settings),
                "status": "complete",
                "collection": "broken_collection",
                "indexed_count": 1,
                "source_signature": "test-source-signature",
            },
        ),
        encoding="utf-8",
    )
    knowledge_registry.mark_published_index_refresh_succeeded(key)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    def _broken_vector_db(*_args: object, **_kwargs: object) -> object:
        msg = "cannot open collection"
        raise RuntimeError(msg)

    monkeypatch.setattr("mindroom.knowledge.registry._build_published_index_vector_db", _broken_vector_db)
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    scheduler.schedule_refresh.assert_called_once()


def test_published_index_handle_open_failure_degrades_and_schedules_repair_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken read handle should not break reply-path knowledge resolution."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    collection = "broken_collection"
    metadata_path = published_index_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "settings": list(key.indexing_settings),
                "status": "complete",
                "collection": collection,
                "indexed_count": 1,
                "source_signature": "test-source-signature",
            },
        ),
        encoding="utf-8",
    )
    _VectorDb.collections[collection] = [
        {
            "content": "last good content",
            "metadata": {"path": "guide.md"},
        },
    ]
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()

    def _broken_vector_db(*_args: object, **_kwargs: object) -> object:
        msg = "cannot open collection"
        raise RuntimeError(msg)

    monkeypatch.setattr("mindroom.knowledge.registry._build_published_index_vector_db", _broken_vector_db)

    resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )

    assert resolution.knowledge is None
    assert resolution.unavailable == {
        "docs": KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.REFRESH_FAILED,
            search_available=False,
        ),
    }
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_first_time_partial_refresh_does_not_publish_ready_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold refresh with incomplete file indexing must not become a last-good index."""
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
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
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
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.indexed_count == 1
    assert result.index_published is False
    assert state is not None
    assert state.status == "indexing"
    assert state.collection is None
    assert knowledge_registry.published_index_refresh_state(state) == "refresh_failed"
    assert state.last_error == "Indexed 1 of 2 managed knowledge files"
    assert lookup.index is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    assert not any("_candidate_" in collection for collection in _VectorDb.collections)


@pytest.mark.asyncio
async def test_cold_refresh_publishes_when_empty_file_produces_no_vectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty managed files should count as scanned without blocking cold index publication."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "useful.md").write_text("useful vectors", encoding="utf-8")
    (docs_path / "empty.md").write_text("", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    class _SkipEmptyKnowledge(_Knowledge):
        async def ainsert(
            self,
            *,
            path: str,
            metadata: dict[str, object],
            upsert: bool,
            reader: object | None = None,
        ) -> None:
            if Path(path).read_text(encoding="utf-8"):
                await super().ainsert(path=path, metadata=metadata, upsert=upsert, reader=reader)

    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _SkipEmptyKnowledge)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.indexed_count == 2
    assert result.index_published is True
    assert result.availability is KnowledgeAvailability.READY
    assert state is not None
    assert state.indexed_count == 2
    assert knowledge_registry.published_index_refresh_state(state) == "none"
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("useful", max_results=5)] == [
        "useful vectors",
    ]


@pytest.mark.asyncio
async def test_embedder_changing_partial_refresh_does_not_publish_old_index_under_new_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial refresh cannot cache old incompatible vectors under a new index key."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("old embedder index", encoding="utf-8")
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
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
    ) -> bool:
        _ = (self, resolved_path, upsert, knowledge, indexed_files, indexed_signatures)
        return False

    monkeypatch.setattr(KnowledgeManager, "_index_file_locked", _partial_candidate)

    result = await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)

    assert result.indexed_count == 0
    assert result.index_published is False
    assert lookup.index is None
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

    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED

    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    first = _resolution.knowledge
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    second = _resolution.knowledge

    assert first is None
    assert second is None
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_refresh_setup_failure_records_failed_availability(tmp_path: Path) -> None:
    """Manager construction failures are persisted instead of leaving cold metadata initializing."""
    docs_path = tmp_path / "docs"
    docs_path.write_text("not a directory", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    with pytest.raises(ValueError, match="must be a directory"):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert state is not None
    assert state.status == "indexing"
    assert state.collection is None
    assert state.refresh_job == "failed"
    assert state.last_error is not None
    assert "must be a directory" in state.last_error
    assert knowledge_registry.published_index_refresh_state(state) == "refresh_failed"
    assert lookup.index is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED


@pytest.mark.asyncio
async def test_api_delete_marks_index_stale_and_keeps_last_good_best_effort(tmp_path: Path) -> None:
    """DELETE success schedules refresh while old vectors remain usable until refresh publishes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("delete me now", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    before_delete = resolve_agent_knowledge_access("helper", config, runtime_paths).knowledge
    assert before_delete is not None
    assert [document.content for document in before_delete.search("delete", max_results=5)] == ["delete me now"]

    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    client = TestClient(main.app)

    response = client.delete("/api/knowledge/bases/docs/files/guide.md")
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    after_delete = _resolution.knowledge

    assert response.status_code == 200
    assert after_delete is not None
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    assert [document.content for document in after_delete.search("delete", max_results=5)] == ["delete me now"]
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_api_replacement_upload_marks_index_stale_and_keeps_last_good_best_effort(
    tmp_path: Path,
) -> None:
    """Replacement uploads leave old vectors usable until refresh publishes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("old upload", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    client = TestClient(main.app)

    response = client.post(
        "/api/knowledge/bases/docs/upload",
        files=[("files", ("guide.md", b"new upload", "text/markdown"))],
    )
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge

    assert response.status_code == 200
    assert knowledge is not None
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    assert [document.content for document in knowledge.search("old upload", max_results=5)] == ["old upload"]
    assert [document.content for document in knowledge.search("new upload", max_results=5)] == ["old upload"]
    scheduler.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_api_upload_failure_does_not_commit_earlier_staged_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed upload batch leaves the source tree and published index unchanged."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("existing upload", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = scheduler
    monkeypatch.setattr("mindroom.api.knowledge._MAX_UPLOAD_BYTES", 5)
    client = TestClient(main.app)

    response = client.post(
        "/api/knowledge/bases/docs/upload",
        files=[
            ("files", ("guide.md", b"small", "text/markdown")),
            ("files", ("new.md", b"too large", "text/markdown")),
        ],
    )

    assert response.status_code == 413
    unavailable: dict[str, KnowledgeAvailability] = {}
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    knowledge = _resolution.knowledge
    assert knowledge is not None
    assert unavailable == {}
    assert (docs_path / "guide.md").read_text(encoding="utf-8") == "existing upload"
    assert not (docs_path / "new.md").exists()
    assert [document.content for document in knowledge.search("existing upload", max_results=5)] == [
        "existing upload",
    ]
    scheduler.schedule_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_api_status_reports_direct_refresh_runner_reindex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status polling should see explicit refresh_knowledge_binding calls, not only scheduler-scheduled jobs."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("refreshing status", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    config_lifecycle.app_state(main.app).knowledge_refresh_scheduler = None
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_reindex(self: KnowledgeManager) -> int:
        _ = self
        started.set()
        await release.wait()
        return 0

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _blocked_reindex)
    refresh_task = asyncio.create_task(
        refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths, force_reindex=True),
    )
    await started.wait()
    try:
        client = TestClient(main.app)
        response = client.get("/api/knowledge/bases/docs/status")
    finally:
        release.set()
        await refresh_task

    assert response.status_code == 200
    assert response.json()["refreshing"] is True


@pytest.mark.asyncio
async def test_refresh_scheduler_runs_independent_per_binding_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduling one binding does not replace, cancel, or wait for another binding."""
    docs_a = tmp_path / "docs-a"
    docs_b = tmp_path / "docs-b"
    config = _config(tmp_path, bases={"a": docs_a, "b": docs_b}, agent_bases=["a", "b"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    started: list[str] = []
    release: dict[str, asyncio.Event] = {"a": asyncio.Event(), "b": asyncio.Event()}

    async def _fake_refresh(base_id: str, **_kwargs: object) -> object:
        started.append(base_id)
        await release[base_id].wait()
        if base_id == "a":
            msg = "a failed"
            raise RuntimeError(msg)
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_scheduler.refresh_knowledge_binding", _fake_refresh)

    scheduler.schedule_refresh("a", config=config, runtime_paths=runtime_paths)
    scheduler.schedule_refresh("a", config=config, runtime_paths=runtime_paths)
    scheduler.schedule_refresh("b", config=config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)

    assert sorted(started) == ["a", "b"]
    assert len(scheduler._tasks) == 2
    release["b"].set()
    await asyncio.sleep(0)
    assert any(key.base_id == "a" for key in scheduler._tasks)
    release["a"].set()
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_refresh_scheduler_coalesces_duplicate_schedule_while_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort schedules run one follow-up refresh with the latest request."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    older_pending_config = config.model_copy(deep=True)
    older_pending_config.knowledge_bases["docs"].chunk_size = 2048
    latest_pending_config = config.model_copy(deep=True)
    latest_pending_config.knowledge_bases["docs"].chunk_size = 4096
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    seen_chunk_sizes: list[int] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def _fake_refresh(_base_id: str, **kwargs: object) -> object:
        _ = _base_id
        refresh_config = kwargs["config"]
        assert isinstance(refresh_config, Config)
        seen_chunk_sizes.append(refresh_config.knowledge_bases["docs"].chunk_size)
        if len(seen_chunk_sizes) == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_scheduler.refresh_knowledge_binding", _fake_refresh)

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await first_started.wait()
    scheduler.schedule_refresh("docs", config=older_pending_config, runtime_paths=runtime_paths)
    scheduler.schedule_refresh("docs", config=latest_pending_config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)

    assert seen_chunk_sizes == [5000]

    release_first.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)
    for _attempt in range(50):
        if not scheduler._tasks:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("coalesced refresh task did not finish")

    assert seen_chunk_sizes == [5000, 4096]


@pytest.mark.asyncio
async def test_refresh_scheduler_refresh_now_runs_directly_with_force_reindex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit refreshes do not go through the best-effort background queue."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    seen_force_reindex: list[bool] = []

    async def _fake_refresh(base_id: str, **kwargs: object) -> object:
        assert base_id == "docs"
        refresh_config = kwargs["config"]
        assert isinstance(refresh_config, Config)
        seen_force_reindex.append(bool(kwargs.get("force_reindex", False)))
        return knowledge_refresh_runner.KnowledgeRefreshResult(
            key=resolve_published_index_key("docs", config=refresh_config, runtime_paths=runtime_paths),
            indexed_count=1,
            index_published=True,
            availability=KnowledgeAvailability.READY,
        )

    monkeypatch.setattr("mindroom.knowledge.refresh_scheduler.refresh_knowledge_binding", _fake_refresh)

    result = await scheduler.refresh_now("docs", config=config, runtime_paths=runtime_paths, force_reindex=True)

    assert result.indexed_count == 1
    assert seen_force_reindex == [True]


@pytest.mark.asyncio
async def test_refresh_scheduler_shutdown_suppresses_completed_refresh_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown drains fire-and-forget refresh task failures instead of re-raising them."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()

    async def _fake_refresh(base_id: str, **_kwargs: object) -> object:
        _ = base_id
        msg = "refresh failed"
        raise RuntimeError(msg)

    monkeypatch.setattr("mindroom.knowledge.refresh_scheduler.refresh_knowledge_binding", _fake_refresh)

    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_refresh_scheduler_does_not_schedule_after_shutdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late schedule calls after shutdown do not create orphaned refresh tasks."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    calls = 0

    async def _fake_refresh(base_id: str, **_kwargs: object) -> object:
        _ = base_id
        nonlocal calls
        calls += 1
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_scheduler.refresh_knowledge_binding", _fake_refresh)

    await scheduler.shutdown()
    scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await asyncio.sleep(0)

    assert calls == 0
    assert scheduler._tasks == {}


@pytest.mark.asyncio
async def test_refresh_status_is_visible_across_scheduler_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard status schedulers should see refreshes started by the Matrix/orchestrator scheduler."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    matrix_scheduler = KnowledgeRefreshScheduler()
    api_scheduler = KnowledgeRefreshScheduler()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_refresh(base_id: str, **_kwargs: object) -> object:
        _ = base_id
        started.set()
        await release.wait()
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_scheduler.refresh_knowledge_binding", _blocked_refresh)

    matrix_scheduler.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await started.wait()

    try:
        assert api_scheduler.is_refreshing("docs", config=config, runtime_paths=runtime_paths) is True
    finally:
        release.set()
        await matrix_scheduler.shutdown()
        await api_scheduler.shutdown()


def test_index_key_is_per_binding_not_raw_base_id(tmp_path: Path) -> None:
    """The same base id resolves to separate refresh keys when storage binding differs."""
    path = tmp_path / "docs"
    config_a = _config(tmp_path / "a", bases={"docs": path}, agent_bases=["docs"])
    config_b = _config(tmp_path / "b", bases={"docs": path}, agent_bases=["docs"])

    key_a = get_published_index("docs", config=config_a, runtime_paths=runtime_paths_for(config_a)).key
    key_b = get_published_index("docs", config=config_b, runtime_paths=runtime_paths_for(config_b)).key

    assert key_a.base_id == key_b.base_id == "docs"
    assert key_a != key_b


@pytest.mark.asyncio
async def test_private_agent_knowledge_publishes_isolated_indexes(tmp_path: Path) -> None:
    """Requester-local private knowledge must resolve to separate physical index bindings."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge"),
                    ),
                ),
            },
            models={},
        ),
        runtime_paths,
    )
    base_id = config.get_agent_private_knowledge_base_id("helper")
    assert base_id is not None
    identity_a = _identity("@alice:localhost")
    identity_b = _identity("@bob:localhost")
    key_a = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity_a,
        create=True,
    )
    key_b = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity_b,
        create=True,
    )
    Path(key_a.knowledge_path).mkdir(parents=True, exist_ok=True)
    Path(key_b.knowledge_path).mkdir(parents=True, exist_ok=True)
    (Path(key_a.knowledge_path) / "note.md").write_text("alice private note", encoding="utf-8")
    (Path(key_b.knowledge_path) / "note.md").write_text("bob private note", encoding="utf-8")

    await refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity_a)
    await refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity_b)
    knowledge_a = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity_a,
    ).knowledge
    knowledge_b = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity_b,
    ).knowledge

    assert key_a != key_b
    assert knowledge_a is not None
    assert knowledge_b is not None
    assert [document.content for document in knowledge_a.search("private", max_results=5)] == ["alice private note"]
    assert [document.content for document in knowledge_b.search("private", max_results=5)] == ["bob private note"]


@pytest.mark.asyncio
async def test_private_agent_knowledge_schedules_refresh_when_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requester-local READY indexes should be served and refreshed without request-time scans."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge"),
                    ),
                ),
            },
            models={},
        ),
        runtime_paths,
    )
    base_id = config.get_agent_private_knowledge_base_id("helper")
    assert base_id is not None
    identity = _identity("@alice:localhost")
    key = resolve_published_index_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
        create=True,
    )
    knowledge_path = Path(key.knowledge_path)
    knowledge_path.mkdir(parents=True, exist_ok=True)
    note = knowledge_path / "note.md"
    note.write_text("alice private old", encoding="utf-8")

    await refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity)
    note.write_text("alice private new", encoding="utf-8")
    scheduler = MagicMock()
    scheduler.is_refreshing = MagicMock(return_value=False)
    scheduler.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}
    unavailable_details: dict[str, KnowledgeAvailabilityDetail] = {}

    def _unexpected_signature(*_args: object, **_kwargs: object) -> str:
        msg = "private READY access should not scan the local corpus"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.knowledge.manager.knowledge_source_signature", _unexpected_signature)
    monkeypatch.setattr(knowledge_utils, "knowledge_source_signature", _unexpected_signature, raising=False)
    _resolution = resolve_agent_knowledge_access(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity,
        refresh_scheduler=scheduler,
    )
    unavailable.update({base_id: detail.availability for (base_id, detail) in _resolution.unavailable.items()})
    unavailable_details.update(_resolution.unavailable)
    knowledge = _resolution.knowledge

    assert knowledge is not None
    assert [document.content for document in knowledge.search("private", max_results=5)] == ["alice private old"]
    assert unavailable == {base_id: KnowledgeAvailability.STALE}
    assert unavailable_details == {
        base_id: KnowledgeAvailabilityDetail(
            availability=KnowledgeAvailability.STALE,
            search_available=True,
        ),
    }
    scheduler.schedule_refresh.assert_called_once()


def test_private_agent_knowledge_bookkeeping_is_bounded(tmp_path: Path) -> None:
    """Private index, lock, and refresh-cooldown registries should be pruned."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge"),
                    ),
                ),
            },
            models={},
        ),
        runtime_paths,
    )
    base_id = config.get_agent_private_knowledge_base_id("helper")
    assert base_id is not None
    max_entries = max(
        knowledge_registry._MAX_PRIVATE_PUBLISHED_INDEXES,
        knowledge_utils._MAX_REFRESH_SCHEDULED_COOLDOWNS,
        knowledge_refresh_runner._MAX_REFRESH_LOCKS,
    )

    for index in range(max_entries + 40):
        identity = _identity(f"@user{index}:localhost")
        key = resolve_published_index_key(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
            create=True,
        )
        collection = f"private_collection_{index}"
        refresh_target = knowledge_registry.refresh_target_for_published_index_key(key)
        knowledge_registry.publish_knowledge_index(
            key,
            knowledge=_Knowledge(_VectorDb(collection=collection)),
            state=knowledge_registry.PublishedIndexState(
                settings=key.indexing_settings,
                status="complete",
                collection=collection,
                source_signature=f"sig-{index}",
            ),
            metadata_path=published_index_metadata_path(key),
        )
        knowledge_utils._refresh_schedule_due(
            refresh_target,
            KnowledgeAvailability.READY,
            settings=key.indexing_settings,
            cooldown_seconds=300,
        )
        _create_idle_refresh_lock(knowledge_registry.source_root_for_refresh_target(refresh_target))

    private_index_count = sum(
        key.base_id.startswith(config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX) for key in knowledge_registry._published_indexes
    )
    assert private_index_count <= knowledge_registry._MAX_PRIVATE_PUBLISHED_INDEXES
    assert len(knowledge_utils._refresh_scheduled_at) <= knowledge_utils._MAX_REFRESH_SCHEDULED_COOLDOWNS
    assert len(knowledge_refresh_runner._refresh_locks) <= knowledge_refresh_runner._MAX_REFRESH_LOCKS


def test_private_index_read_path_cache_insertion_is_bounded(tmp_path: Path) -> None:
    """Loading persisted private indexes through the read path should prune old cache entries."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(
                    display_name="Helper",
                    private=AgentPrivateConfig(
                        per="user",
                        root="mind_data",
                        knowledge=AgentPrivateKnowledgeConfig(path="knowledge"),
                    ),
                ),
            },
            models={},
        ),
        runtime_paths,
    )
    base_id = config.get_agent_private_knowledge_base_id("helper")
    assert base_id is not None
    count = knowledge_registry._MAX_PRIVATE_PUBLISHED_INDEXES + 10

    for index in range(count):
        identity = _identity(f"@user{index}:localhost")
        key = resolve_published_index_key(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
            create=True,
        )
        collection = f"private_read_collection_{index}"
        _VectorDb.collections[collection] = [
            {"content": f"private read {index}", "metadata": {"source_path": "note.md"}},
        ]
        knowledge_registry.save_published_index_state(
            published_index_metadata_path(key),
            knowledge_registry.PublishedIndexState(
                settings=key.indexing_settings,
                status="complete",
                collection=collection,
                indexed_count=1,
                source_signature=f"sig-{index}",
            ),
        )
        knowledge_registry.mark_published_index_refresh_succeeded(key)

    clear_published_indexes()

    for index in range(count):
        lookup = get_published_index(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=_identity(f"@user{index}:localhost"),
        )
        assert lookup.index is not None

    private_index_count = sum(
        key.base_id.startswith(config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX) for key in knowledge_registry._published_indexes
    )
    assert private_index_count <= knowledge_registry._MAX_PRIVATE_PUBLISHED_INDEXES


def test_publish_knowledge_index_caches_handle_without_collection_leases(tmp_path: Path) -> None:
    """Published indexs use only the active cache, not reader lease bookkeeping."""

    class _NonWeakrefKnowledge:
        __slots__ = ()

    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge = _NonWeakrefKnowledge()

    index = knowledge_registry.publish_knowledge_index(
        key,
        knowledge=knowledge,
        state=knowledge_registry.PublishedIndexState(
            settings=key.indexing_settings,
            status="complete",
            collection="non_weakref_collection",
        ),
        metadata_path=published_index_metadata_path(key),
    )

    assert knowledge_registry._published_indexes[key] is index


@pytest.mark.asyncio
async def test_published_indexed_count_uses_persisted_metadata_without_collection_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routine status counts come from metadata rather than scanning vector rows."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None

    def _raise_scan(self: _Client, name: str) -> _Collection:
        _ = (self, name)
        msg = "collection scan should not be used"
        raise AssertionError(msg)

    monkeypatch.setattr(_Client, "get_collection", _raise_scan)

    assert published_indexed_count(lookup.index) == 1


@pytest.mark.asyncio
async def test_local_noop_refresh_reports_published_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchanged local refresh republishes a usable index and reports it as published."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("local index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _track_reindex(self: KnowledgeManager) -> int:
        nonlocal reindex_count
        reindex_count += 1
        if reindex_count > 1:
            msg = "unchanged local refresh should not reindex"
            raise AssertionError(msg)
        return await original_reindex(self)

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert result.indexed_count == 1
    assert reindex_count == 1


@pytest.mark.asyncio
async def test_local_refresh_reindexes_when_content_changes_with_same_mtime_and_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unchanged fast path must not publish stale vectors after content-only changes."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("old index", encoding="utf-8")
    initial_stat = doc.stat()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _track_reindex(self: KnowledgeManager) -> int:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self)

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("new index", encoding="utf-8")
    os.utime(doc, ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns))
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert reindex_count == 2
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("index", max_results=5)] == [
        "new index",
    ]


@pytest.mark.asyncio
async def test_refresh_does_not_synthesize_missing_published_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing publish pointer after refresh leaves published unavailable instead of creating READY metadata."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("metadata index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    original_reindex = KnowledgeManager.reindex_all

    async def _delete_metadata_after_reindex(self: KnowledgeManager) -> int:
        indexed_count = await original_reindex(self)
        self._indexing_settings_path.unlink()
        return indexed_count

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _delete_metadata_after_reindex)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))

    assert result.index_published is False
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert state is not None
    assert state.status == "failed"
    assert state.collection is None
    assert state.last_error == "Published index metadata was missing after refresh"
    assert knowledge_registry.published_index_refresh_state(state) == "refresh_failed"


def test_published_metadata_write_uses_unique_temp_and_cleans_failed_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published metadata writes should not share one deterministic temp file."""
    metadata_path = tmp_path / "indexing_settings.json"
    attempted_temp_paths: list[Path] = []
    original_replace = Path.replace

    def _fail_temp_replace(self: Path, target: Path) -> Path:
        if self.parent == tmp_path and self.name.startswith(".indexing_settings.json.") and self.name.endswith(".tmp"):
            attempted_temp_paths.append(self)
            msg = "replace failed"
            raise OSError(msg)
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _fail_temp_replace)

    with pytest.raises(OSError, match="replace failed"):
        knowledge_registry.save_published_index_state(
            metadata_path,
            knowledge_registry.PublishedIndexState(
                settings=("settings",),
                status="complete",
                collection="collection",
                source_signature="signature",
            ),
        )

    assert attempted_temp_paths
    assert attempted_temp_paths[0].name != "indexing_settings.json.tmp"
    assert not attempted_temp_paths[0].exists()


@pytest.mark.asyncio
async def test_git_refresh_syncs_before_reindex_and_publishes_revision_without_secret_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed refresh syncs first, publishes the revision, and persists no URL userinfo."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git index", encoding="utf-8")
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

    async def _sync_success(self: KnowledgeManager) -> dict[str, object]:
        order.append("sync")
        self._git_last_successful_commit = "rev-git"
        _set_git_tracked_files(self, "doc.md")
        return {"updated": True, "changed_count": 1, "removed_count": 0}

    async def _track_reindex(self: KnowledgeManager) -> int:
        order.append("reindex")
        return await original_reindex(self)

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync_success)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    metadata_text = published_index_metadata_path(key).read_text(encoding="utf-8")

    assert result.index_published is True
    assert order == ["sync", "reindex"]
    assert state is not None
    assert state.published_revision == "rev-git"
    assert state.source_signature == knowledge_source_signature(
        config,
        "docs",
        docs_path,
        tracked_relative_paths={"doc.md"},
    )
    assert "ghp_secret" not in metadata_text
    assert "x-oauth-basic" not in metadata_text


@pytest.mark.asyncio
async def test_git_noop_refresh_skips_full_reindex_when_index_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged Git poll should update sync metadata without rebuilding the collection."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git index", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    sync_results = [
        {"updated": True, "changed_count": 1, "removed_count": 0, "commit": "rev-a"},
        {"updated": False, "changed_count": 0, "removed_count": 0, "commit": "rev-b"},
    ]
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _sync(self: KnowledgeManager) -> dict[str, object]:
        result = sync_results.pop(0)
        self._git_last_successful_commit = str(result["commit"])
        _set_git_tracked_files(self, "doc.md")
        return result

    async def _track_reindex(self: KnowledgeManager) -> int:
        nonlocal reindex_count
        reindex_count += 1
        if reindex_count > 1:
            msg = "unchanged git poll should not reindex"
            raise AssertionError(msg)
        return await original_reindex(self)

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state_before_noop = load_published_index_state(published_index_metadata_path(key))
    assert state_before_noop is not None
    assert state_before_noop.published_revision == "rev-a"
    await asyncio.sleep(0.001)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    state_after_noop = load_published_index_state(published_index_metadata_path(key))

    assert result.index_published is True
    assert result.indexed_count == 1
    assert state_after_noop is not None
    assert state_after_noop.collection == state_before_noop.collection
    assert state_after_noop.published_revision == "rev-b"
    assert state_after_noop.last_published_at is not None
    assert state_after_noop.last_published_at != state_before_noop.last_published_at
    assert reindex_count == 1


@pytest.mark.asyncio
async def test_git_noop_refresh_ignores_untracked_indexable_file_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed corpora use tracked files only and ignore untracked checkout files."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git tracked index", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    sync_results = [
        {"updated": True, "changed_count": 1, "removed_count": 0, "commit": "rev-a"},
        {"updated": False, "changed_count": 0, "removed_count": 0, "commit": "rev-a"},
    ]
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _sync(self: KnowledgeManager) -> dict[str, object]:
        result = sync_results.pop(0)
        self._git_last_successful_commit = str(result["commit"])
        _set_git_tracked_files(self, "doc.md")
        return result

    async def _track_reindex(self: KnowledgeManager) -> int:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self)

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / "untracked.md").write_text("git untracked local corpus", encoding="utf-8")
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert reindex_count == 1
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("git", max_results=5)] == [
        "git tracked index",
    ]


@pytest.mark.asyncio
async def test_git_noop_refresh_rebuilds_when_collection_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged Git poll must not let Agno auto-create a Chroma collection for a missing index."""
    monkeypatch.setattr("mindroom.knowledge.manager.Knowledge", _AutoCreatingKnowledge)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git repaired", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    sync_results = [
        {"updated": True, "changed_count": 1, "removed_count": 0, "commit": "rev-a"},
        {"updated": False, "changed_count": 0, "removed_count": 0, "commit": "rev-a"},
    ]
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _sync(self: KnowledgeManager) -> dict[str, object]:
        result = sync_results.pop(0)
        self._git_last_successful_commit = str(result["commit"])
        _set_git_tracked_files(self, "doc.md")
        return result

    async def _track_reindex(self: KnowledgeManager) -> int:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self)

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    assert state is not None
    assert state.collection is not None
    missing_collection = state.collection
    _VectorDb.collections.pop(missing_collection, None)
    clear_published_indexes()
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    repaired_state = load_published_index_state(published_index_metadata_path(key))

    assert result.index_published is True
    assert reindex_count == 2
    assert repaired_state is not None
    assert repaired_state.collection != missing_collection
    assert missing_collection not in _VectorDb.collections
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("git", max_results=5)] == [
        "git repaired",
    ]


@pytest.mark.asyncio
async def test_unchanged_refresh_fails_when_publish_handle_rebuild_returns_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no-op path must not claim success without a usable published read handle."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("unchanged index", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    monkeypatch.setattr(knowledge_refresh_runner, "publish_knowledge_index_from_state", lambda *_args, **_kwargs: None)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))

    assert result.index_published is False
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert result.last_error == "Published index collection was missing during unchanged refresh"
    assert state is not None
    assert knowledge_registry.published_index_refresh_state(state) == "refresh_failed"
    assert state.refresh_job == "failed"


@pytest.mark.asyncio
async def test_git_noop_refresh_rebuilds_after_chunking_config_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunking changes must rebuild even when Git reports no repository updates."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("git chunking old", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    sync_results = [
        {"updated": True, "changed_count": 1, "removed_count": 0, "commit": "rev-a"},
        {"updated": False, "changed_count": 0, "removed_count": 0, "commit": "rev-a"},
    ]
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _sync(self: KnowledgeManager) -> dict[str, object]:
        result = sync_results.pop(0)
        self._git_last_successful_commit = str(result["commit"])
        _set_git_tracked_files(self, "doc.md")
        return result

    async def _track_reindex(self: KnowledgeManager) -> int:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self)

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("git chunking rebuilt", encoding="utf-8")
    result = await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert reindex_count == 2
    lookup = get_published_index("docs", config=changed_config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("git", max_results=5)] == [
        "git chunking rebuilt",
    ]


@pytest.mark.asyncio
async def test_force_git_reindex_bypasses_noop_fast_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit reindex should rebuild even when Git reports updated=False."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("git force old", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    sync_results = [
        {"updated": True, "changed_count": 1, "removed_count": 0, "commit": "rev-a"},
        {"updated": False, "changed_count": 0, "removed_count": 0, "commit": "rev-a"},
    ]
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _sync(self: KnowledgeManager) -> dict[str, object]:
        result = sync_results.pop(0)
        self._git_last_successful_commit = str(result["commit"])
        _set_git_tracked_files(self, "doc.md")
        return result

    async def _track_reindex(self: KnowledgeManager) -> int:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self)

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("git force rebuilt", encoding="utf-8")
    result = await refresh_knowledge_binding(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        force_reindex=True,
    )

    assert result.index_published is True
    assert reindex_count == 2
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.index is not None
    assert [document.content for document in lookup.index.knowledge.search("git", max_results=5)] == [
        "git force rebuilt",
    ]


@pytest.mark.asyncio
async def test_git_sync_failure_preserves_last_good_index_and_redacts_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git sync failure keeps the last-good index available under stale metadata."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("stable git index", encoding="utf-8")
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

    async def _sync_success(self: KnowledgeManager) -> dict[str, object]:
        self._git_last_successful_commit = "rev-ok"
        _set_git_tracked_files(self, "doc.md")
        return {"updated": True, "changed_count": 1, "removed_count": 0}

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync_success)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    async def _sync_failure(self: KnowledgeManager) -> dict[str, object]:
        _ = self
        msg = "fetch failed https://ghp_secret:x-oauth-basic@example.com/org/repo.git"
        raise RuntimeError(msg)

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync_failure)
    with pytest.raises(RuntimeError, match="fetch failed"):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert state is not None
    assert knowledge_registry.published_index_refresh_state(state) == "refresh_failed"
    assert state.last_error is not None
    assert "ghp_secret" not in state.last_error
    assert "x-oauth-basic" not in state.last_error
    assert lookup.index is not None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    assert [document.content for document in lookup.index.knowledge.search("index", max_results=5)] == [
        "stable git index",
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

    async def _sync_failure(self: KnowledgeManager) -> dict[str, object]:
        _ = self
        msg = "clone failed https://ghp_secret:x-oauth-basic@example.com/org/repo.git"
        raise RuntimeError(msg)

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync_failure)

    with pytest.raises(RuntimeError, match="clone failed"):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)

    assert state is not None
    assert state.status == "indexing"
    assert state.collection is None
    assert state.refresh_job == "failed"
    assert state.last_error is not None
    assert "ghp_secret" not in state.last_error
    assert "x-oauth-basic" not in state.last_error
    assert knowledge_registry.published_index_refresh_state(state) == "refresh_failed"
    assert lookup.index is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED


@pytest.mark.asyncio
async def test_git_failure_redacts_authorization_headers_from_raised_and_metadata_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process-local Git Authorization headers should not leak through command failures."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    git_config = KnowledgeGitConfig(
        repo_url="https://example.com/org/repo.git",
        branch="main",
        credentials_service="github_private",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
        "github_private",
        {"token": "secret-token"},
    )
    encoded = base64.b64encode(b"x-access-token:secret-token").decode("ascii")
    bearer_value = "bearer-value"
    stderr = (
        "fatal: clone failed\n"
        f"GIT_CONFIG_VALUE_0=Authorization: Basic {encoded}\n"
        "decoded credential x-access-token:secret-token\n"
        f"Authorization: Bearer {bearer_value}\n"
    )

    class _FailedGitProcess:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", stderr.encode()

        def kill(self) -> None:
            return None

        async def wait(self) -> None:
            return None

    async def _fail_git_command(*args: object, **kwargs: object) -> _FailedGitProcess:
        _ = (args, kwargs)
        return _FailedGitProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fail_git_command)

    with pytest.raises(RuntimeError) as exc_info:
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    lookup = get_published_index("docs", config=config, runtime_paths=runtime_paths)
    assert state is not None
    assert lookup.state is not None
    error_texts = [str(exc_info.value), state.last_error or "", lookup.state.last_error or ""]

    assert knowledge_registry.published_index_refresh_state(state) == "refresh_failed"
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    for error_text in error_texts:
        assert "Authorization: Basic ***" in error_text
        assert "Authorization: Bearer ***" in error_text
        assert encoded not in error_text
        assert "x-access-token:secret-token" not in error_text
        assert "secret-token" not in error_text
        assert bearer_value not in error_text


@pytest.mark.asyncio
async def test_git_refresh_marks_duplicate_source_sibling_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Git update for one base must not leave sibling indexes READY for the old checkout."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("shared git old", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"alpha": docs_path, "beta": docs_path},
        agent_bases=["alpha", "beta"],
        git_configs={"alpha": git_config, "beta": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_updated(self: KnowledgeManager) -> dict[str, object]:
        self._git_last_successful_commit = f"rev-{self.base_id}"
        _set_git_tracked_files(self, "doc.md")
        return {"updated": True, "changed_count": 1, "removed_count": 0}

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync_updated)

    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)
    assert beta_lookup.index is not None
    assert beta_lookup.availability is KnowledgeAvailability.READY
    assert [document.content for document in beta_lookup.index.knowledge.search("git", max_results=5)] == [
        "shared git old",
    ]

    doc.write_text("shared git new", encoding="utf-8")
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    beta_key = resolve_published_index_key("beta", config=config, runtime_paths=runtime_paths)
    beta_state = load_published_index_state(published_index_metadata_path(beta_key))
    refreshed_beta_lookup = get_published_index("beta", config=config, runtime_paths=runtime_paths)

    assert beta_state is not None
    assert knowledge_registry.published_index_refresh_state(beta_state) == "stale"
    assert refreshed_beta_lookup.index is not None
    assert refreshed_beta_lookup.availability is KnowledgeAvailability.STALE
    assert [document.content for document in refreshed_beta_lookup.index.knowledge.search("git", max_results=5)] == [
        "shared git old",
    ]


@pytest.mark.asyncio
async def test_git_credentials_service_token_stays_out_of_git_config_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CredentialsManager Git secrets should be process-local, not copied into checkout config."""
    docs_path = tmp_path / "docs"
    git_config = KnowledgeGitConfig(
        repo_url="https://example.com/org/private.git",
        branch="main",
        credentials_service="github_private",
    )
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    get_runtime_shared_credentials_manager(runtime_paths).save_credentials(
        "github_private",
        {"token": "secret-token"},
    )
    clone_envs: list[dict[str, str] | None] = []
    clean_url = "https://example.com/org/private.git"

    async def _fake_run_git(
        self: KnowledgeManager,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = self
        if args[0] == "clone":
            clone_envs.append(env)
            assert args[-2] == clean_url
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                subprocess.run,
                ["git", "init"],
                cwd=target,
                check=True,
                capture_output=True,
                text=True,
            )
            await asyncio.to_thread(
                subprocess.run,
                ["git", "remote", "add", "origin", args[-2]],
                cwd=target,
                check=True,
                capture_output=True,
                text=True,
            )
            (target / "doc.md").write_text("credential service content", encoding="utf-8")
            return ""
        if args == ["remote", "set-url", "origin", clean_url]:
            assert cwd is not None
            await asyncio.to_thread(
                subprocess.run,
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            )
            return ""
        if args == ["ls-files", "-z"]:
            return "doc.md\x00"
        if args == ["rev-parse", "HEAD"]:
            return "rev-auth\n"
        return ""

    monkeypatch.setattr(KnowledgeManager, "_run_git", _fake_run_git)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_text = published_index_metadata_path(key).read_text(encoding="utf-8")
    git_config_text = (docs_path / ".git" / "config").read_text(encoding="utf-8")
    clone_env = clone_envs[0]

    assert result.index_published is True
    assert clone_env is not None
    assert clone_env["GIT_CONFIG_KEY_0"] == f"http.{clean_url}.extraHeader"
    assert clone_env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "secret-token" not in str(clone_env)
    assert "secret-token" not in git_config_text
    assert "x-access-token" not in git_config_text
    assert clean_url in git_config_text
    assert "secret-token" not in metadata_text
    assert "x-access-token" not in metadata_text


@pytest.mark.asyncio
async def test_git_embedded_userinfo_url_is_not_reused_in_git_auth_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Embedded Git URL userinfo should become process-local auth without echoing the raw URL."""
    docs_path = tmp_path / "docs"
    raw_url = "https://git-user:secret-token@example.com/org/private.git"
    clean_url = "https://example.com/org/private.git"
    git_config = KnowledgeGitConfig(repo_url=raw_url, branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    clone_envs: list[dict[str, str] | None] = []

    async def _fake_run_git(
        self: KnowledgeManager,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = (self, cwd)
        if args[0] == "clone":
            clone_envs.append(env)
            assert args[-2] == clean_url
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "doc.md").write_text("embedded userinfo content", encoding="utf-8")
            return ""
        if args == ["remote", "set-url", "origin", clean_url]:
            return ""
        if args == ["ls-files", "-z"]:
            return "doc.md\x00"
        if args == ["rev-parse", "HEAD"]:
            return "rev-userinfo\n"
        return ""

    monkeypatch.setattr(KnowledgeManager, "_run_git", _fake_run_git)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    clone_env = clone_envs[0]

    assert result.index_published is True
    assert clone_env is not None
    assert clone_env["GIT_CONFIG_KEY_0"] == f"http.{clean_url}.extraHeader"
    assert clone_env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert raw_url not in str(clone_env)
    assert "secret-token" not in str(clone_env)


@pytest.mark.parametrize(
    ("raw_url", "clean_url"),
    [
        ("ssh://git-user:secret-token@example.com/org/private.git", "ssh://example.com/org/private.git"),
        ("git+https://git-user:secret-token@example.com/org/private.git", "git+https://example.com/org/private.git"),
    ],
)
@pytest.mark.asyncio
async def test_git_unsupported_scheme_userinfo_is_not_copied_to_git_config_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_url: str,
    clean_url: str,
) -> None:
    """Unsupported embedded userinfo must not be copied into transient Git config."""
    docs_path = tmp_path / "docs"
    git_config = KnowledgeGitConfig(repo_url=raw_url, branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    clone_calls: list[tuple[list[str], dict[str, str] | None]] = []

    async def _fake_run_git(
        self: KnowledgeManager,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = (self, cwd)
        if args[0] == "clone":
            clone_calls.append((list(args), env))
            assert args[-2] == clean_url
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            (target / "doc.md").write_text("unsupported scheme userinfo content", encoding="utf-8")
            return ""
        if args == ["remote", "set-url", "origin", clean_url]:
            return ""
        if args == ["ls-files", "-z"]:
            return "doc.md\x00"
        if args == ["rev-parse", "HEAD"]:
            return "rev-unsupported-userinfo\n"
        return ""

    monkeypatch.setattr(KnowledgeManager, "_run_git", _fake_run_git)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_text = published_index_metadata_path(key).read_text(encoding="utf-8")
    clone_args, clone_env = clone_calls[0]
    serialized_clone_call = json.dumps({"args": clone_args, "env": clone_env}, sort_keys=True)

    assert result.index_published is True
    assert clone_env is None
    assert clean_url in clone_args
    assert raw_url not in serialized_clone_call
    assert "secret-token" not in serialized_clone_call
    assert raw_url not in metadata_text
    assert "secret-token" not in metadata_text


@pytest.mark.asyncio
async def test_git_query_and_fragment_tokens_stay_out_of_persistent_remote_and_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL query and fragment secrets should be transient auth only, never persisted."""
    docs_path = tmp_path / "docs"
    raw_url = "https://example.com/org/private.git?token=query-secret#frag-secret"
    clean_url = "https://example.com/org/private.git"
    git_config = KnowledgeGitConfig(repo_url=raw_url, branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    clone_envs: list[dict[str, str] | None] = []

    async def _fake_run_git(
        self: KnowledgeManager,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = self
        if args[0] == "clone":
            clone_envs.append(env)
            assert args[-2] == clean_url
            target = Path(args[-1])
            target.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                subprocess.run,
                ["git", "init"],
                cwd=target,
                check=True,
                capture_output=True,
                text=True,
            )
            await asyncio.to_thread(
                subprocess.run,
                ["git", "remote", "add", "origin", args[-2]],
                cwd=target,
                check=True,
                capture_output=True,
                text=True,
            )
            (target / "doc.md").write_text("query credential content", encoding="utf-8")
            return ""
        if args == ["remote", "set-url", "origin", clean_url]:
            assert cwd is not None
            await asyncio.to_thread(
                subprocess.run,
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            )
            return ""
        if args == ["ls-files", "-z"]:
            return "doc.md\x00"
        if args == ["rev-parse", "HEAD"]:
            return "rev-query\n"
        return ""

    monkeypatch.setattr(KnowledgeManager, "_run_git", _fake_run_git)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_published_index_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_text = published_index_metadata_path(key).read_text(encoding="utf-8")
    git_config_text = (docs_path / ".git" / "config").read_text(encoding="utf-8")

    assert result.index_published is True
    assert clone_envs
    assert "query-secret" in str(clone_envs[0])
    assert "frag-secret" in str(clone_envs[0])
    assert clean_url in git_config_text
    assert "query-secret" not in git_config_text
    assert "frag-secret" not in git_config_text
    assert "query-secret" not in metadata_text
    assert "frag-secret" not in metadata_text
    assert redact_url_credentials(config.knowledge_bases["docs"].git.repo_url) == clean_url


@pytest.mark.asyncio
async def test_existing_single_branch_checkout_switches_to_new_remote_branch(tmp_path: Path) -> None:
    """A checkout cloned for one branch should fetch and switch to another configured branch."""
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()

    async def _git(cwd: Path, *args: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    await _git(remote_work, "init", "-b", "main")
    await _git(remote_work, "config", "user.email", "tests@example.com")
    await _git(remote_work, "config", "user.name", "MindRoom Tests")
    (remote_work / "doc.md").write_text("main branch content", encoding="utf-8")
    await _git(remote_work, "add", "doc.md")
    await _git(remote_work, "commit", "-m", "main")
    await _git(remote_work, "checkout", "-b", "release")
    (remote_work / "doc.md").write_text("release branch content", encoding="utf-8")
    await _git(remote_work, "commit", "-am", "release")
    remote_bare = tmp_path / "remote.git"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "--bare", str(remote_work), str(remote_bare)],
        check=True,
        capture_output=True,
        text=True,
    )

    docs_path = tmp_path / "checkout"
    main_config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url=str(remote_bare), branch="main")},
    )
    runtime_paths = runtime_paths_for(main_config)
    await refresh_knowledge_binding("docs", config=main_config, runtime_paths=runtime_paths)
    main_lookup = get_published_index("docs", config=main_config, runtime_paths=runtime_paths)
    assert main_lookup.index is not None
    assert [document.content for document in main_lookup.index.knowledge.search("branch", max_results=5)] == [
        "main branch content",
    ]

    release_config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": KnowledgeGitConfig(repo_url=str(remote_bare), branch="release")},
    )
    result = await refresh_knowledge_binding(
        "docs",
        config=release_config,
        runtime_paths=runtime_paths,
        force_reindex=True,
    )
    release_lookup = get_published_index("docs", config=release_config, runtime_paths=runtime_paths)

    assert result.index_published is True
    assert release_lookup.index is not None
    assert [document.content for document in release_lookup.index.knowledge.search("branch", max_results=5)] == [
        "release branch content",
    ]


@pytest.mark.asyncio
async def test_git_worktree_checkout_file_is_detected_for_sync_listing_and_api_status(tmp_path: Path) -> None:
    """Git worktree checkouts use a .git file and must still count as present repositories."""
    remote_work = tmp_path / "remote-work"
    remote_work.mkdir()

    async def _git(cwd: Path, *args: str) -> None:
        await asyncio.to_thread(
            subprocess.run,
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    await _git(remote_work, "init", "-b", "main")
    await _git(remote_work, "config", "user.email", "tests@example.com")
    await _git(remote_work, "config", "user.name", "MindRoom Tests")
    (remote_work / "doc.md").write_text("worktree checkout content", encoding="utf-8")
    await _git(remote_work, "add", "doc.md")
    await _git(remote_work, "commit", "-m", "main")
    remote_bare = tmp_path / "remote.git"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", "--bare", str(remote_work), str(remote_bare)],
        check=True,
        capture_output=True,
        text=True,
    )
    seed_checkout = tmp_path / "seed-checkout"
    await asyncio.to_thread(
        subprocess.run,
        ["git", "clone", str(remote_bare), str(seed_checkout)],
        check=True,
        capture_output=True,
        text=True,
    )
    docs_path = tmp_path / "worktree-checkout"
    await _git(seed_checkout, "worktree", "add", "--detach", str(docs_path), "HEAD")
    assert (docs_path / ".git").is_file()

    git_config = KnowledgeGitConfig(repo_url=str(remote_bare), branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    resolved_git_config = manager._git_config()
    assert resolved_git_config is not None

    cloned = await manager._ensure_git_repository(resolved_git_config)

    assert cloned is False
    assert git_checkout_present(docs_path)
    assert manager._git_repo_present is True
    assert list_git_tracked_knowledge_files(config, "docs", docs_path) == [docs_path.resolve() / "doc.md"]

    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    client = TestClient(main.app)
    response = client.get("/api/knowledge/bases/docs/status")

    assert response.status_code == 200
    assert response.json()["git"]["repo_present"] is True


@pytest.mark.asyncio
async def test_candidate_indexing_hashes_content_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-file content hashing should run in a worker thread."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("threaded hash", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    event_loop_thread = get_ident()
    signature_threads: list[int] = []
    original_file_signature = KnowledgeManager._file_signature

    def _record_signature_thread(self: KnowledgeManager, file_path: Path) -> tuple[int, int, str]:
        signature_threads.append(get_ident())
        return original_file_signature(self, file_path)

    monkeypatch.setattr(KnowledgeManager, "_file_signature", _record_signature_thread)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert signature_threads
    assert all(thread_id != event_loop_thread for thread_id in signature_threads)


@pytest.mark.asyncio
async def test_git_updated_stale_registry_mark_uses_async_registry_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh runner should mark stale metadata off the event loop."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git updated", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", branch="main")
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    event_loop_thread = get_ident()
    mark_threads: list[int] = []

    async def _sync_updated(self: KnowledgeManager) -> dict[str, object]:
        self._git_last_successful_commit = "rev-updated"
        _set_git_tracked_files(self, "doc.md")
        return {"updated": True, "changed_count": 1, "removed_count": 0}

    async def _record_mark_thread(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        mark_threads.append(get_ident())
        return ("docs",)

    monkeypatch.setattr(KnowledgeManager, "sync_git_source", _sync_updated)
    monkeypatch.setattr(knowledge_refresh_runner, "mark_knowledge_source_changed_async", _record_mark_thread)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert mark_threads == [event_loop_thread]


@pytest.mark.asyncio
async def test_refresh_scheduler_manual_reindex_runs_without_background_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An awaited manual refresh should bypass duplicate best-effort background schedules."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    old_config = config.model_copy(deep=True)
    old_config.knowledge_bases["docs"].chunk_size = 1024
    runtime_paths = runtime_paths_for(config)
    scheduler = KnowledgeRefreshScheduler()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    seen: list[tuple[int, bool]] = []

    async def _fake_refresh(base_id: str, **kwargs: object) -> object:
        assert base_id == "docs"
        refresh_config = kwargs["config"]
        assert isinstance(refresh_config, Config)
        force_reindex = bool(kwargs.get("force_reindex", False))
        seen.append((refresh_config.knowledge_bases["docs"].chunk_size, force_reindex))
        if len(seen) == 1:
            first_started.set()
            await release_first.wait()
        return knowledge_refresh_runner.KnowledgeRefreshResult(
            key=resolve_published_index_key("docs", config=refresh_config, runtime_paths=runtime_paths),
            indexed_count=1,
            index_published=True,
            availability=KnowledgeAvailability.READY,
        )

    monkeypatch.setattr("mindroom.knowledge.refresh_scheduler.refresh_knowledge_binding", _fake_refresh)

    scheduler.schedule_refresh("docs", config=old_config, runtime_paths=runtime_paths)
    await first_started.wait()
    scheduler.schedule_refresh("docs", config=old_config, runtime_paths=runtime_paths)
    manual_task = asyncio.create_task(
        scheduler.refresh_now("docs", config=config, runtime_paths=runtime_paths, force_reindex=True),
    )
    await asyncio.sleep(0)
    release_first.set()
    await manual_task
    for _attempt in range(50):
        if not scheduler._tasks:
            break
        await asyncio.sleep(0)
    else:
        pytest.fail("manual refresh left a stale background refresh running")
    await scheduler.shutdown()

    assert seen == [(1024, False), (5000, True)]


@pytest.mark.asyncio
async def test_sync_git_source_once_skips_repeated_lfs_pull_for_already_hydrated_unchanged_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged LFS heads should hydrate once, then reuse the persisted hydration marker."""
    manager = _git_manager(tmp_path, lfs=True)
    git_calls: list[list[str]] = []

    async def _fake_ensure_git_repository(_git_config: object) -> bool:
        return False

    async def _fake_git_rev_parse(ref: str) -> str | None:
        if ref in {"HEAD", "origin/main"}:
            return "same"
        return None

    async def _fake_git_list_tracked_files() -> set[str]:
        return {"doc.md"}

    async def _fake_run_git(args: list[str], **_: object) -> str:
        git_calls.append(args)
        return ""

    monkeypatch.setattr(manager, "_ensure_git_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(manager, "_git_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(manager, "_git_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(manager, "_run_git", _fake_run_git)

    changed_files, removed_files, updated = await manager._sync_git_source_once(manager._git_config())

    assert updated is False
    assert changed_files == set()
    assert removed_files == set()
    assert ["lfs", "pull", "origin", "main"] in git_calls

    hydrated_manager = _git_manager(tmp_path, lfs=True)
    repeated_git_calls: list[list[str]] = []

    async def _fake_run_git_second(args: list[str], **_: object) -> str:
        repeated_git_calls.append(args)
        return ""

    monkeypatch.setattr(hydrated_manager, "_ensure_git_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(hydrated_manager, "_git_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(hydrated_manager, "_git_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(hydrated_manager, "_run_git", _fake_run_git_second)

    changed_files, removed_files, updated = await hydrated_manager._sync_git_source_once(
        hydrated_manager._git_config(),
    )

    assert updated is False
    assert changed_files == set()
    assert removed_files == set()
    assert ["lfs", "pull", "origin", "main"] not in repeated_git_calls


@pytest.mark.asyncio
async def test_sync_git_source_once_pulls_lfs_after_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LFS-enabled repos should explicitly pull LFS objects after resetting to the remote branch."""
    manager = _git_manager(tmp_path, lfs=True)
    git_calls: list[list[str]] = []

    async def _fake_ensure_git_repository(_git_config: object) -> bool:
        return False

    async def _fake_git_rev_parse(ref: str) -> str | None:
        if ref == "HEAD":
            return "before"
        if ref == "origin/main":
            return "after"
        return None

    list_tracked_files_results = iter([{"doc.md"}, {"doc.md"}])

    async def _fake_git_list_tracked_files() -> set[str]:
        return next(list_tracked_files_results)

    async def _fake_run_git(args: list[str], **_: object) -> str:
        git_calls.append(args)
        if args[:3] == ["diff", "--name-only", "--no-renames"]:
            return "doc.md\n"
        return ""

    monkeypatch.setattr(manager, "_ensure_git_repository", _fake_ensure_git_repository)
    monkeypatch.setattr(manager, "_git_rev_parse", _fake_git_rev_parse)
    monkeypatch.setattr(manager, "_git_list_tracked_files", _fake_git_list_tracked_files)
    monkeypatch.setattr(manager, "_run_git", _fake_run_git)

    changed_files, removed_files, updated = await manager._sync_git_source_once(manager._git_config())

    assert updated is True
    assert changed_files == {"doc.md"}
    assert removed_files == set()
    assert ["lfs", "pull", "origin", "main"] in git_calls


@pytest.mark.asyncio
async def test_ensure_git_lfs_available_raises_clear_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing Git LFS should raise the runtime-image guidance instead of a raw git failure."""
    manager = _git_manager(tmp_path, lfs=True)

    async def _fake_run_git(args: list[str], **_: object) -> str:
        if args == ["lfs", "version"]:
            msg = "git: 'lfs' is not a git command"
            raise RuntimeError(msg)
        return ""

    monkeypatch.setattr(manager, "_run_git", _fake_run_git)

    with pytest.raises(RuntimeError, match="Git LFS is required for this knowledge base"):
        await manager._ensure_git_lfs_available(cwd=manager.knowledge_path)


@pytest.mark.asyncio
async def test_ensure_git_repository_clones_lfs_repo_with_skip_smudge_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial LFS clones should hydrate even if an old hydrated-head marker matches the cloned commit."""
    manager = _git_manager(tmp_path, lfs=True)
    clone_envs: list[dict[str, str] | None] = []
    git_calls: list[list[str]] = []
    manager._git_lfs_hydrated_head_path.write_text("same", encoding="utf-8")

    async def _fake_run_git(
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = cwd
        git_calls.append(args)
        if args[0] == "clone":
            clone_envs.append(env)
        return ""

    async def _fake_git_rev_parse(_ref: str) -> str | None:
        return "same"

    monkeypatch.setattr(manager, "_run_git", _fake_run_git)
    monkeypatch.setattr(manager, "_git_rev_parse", _fake_git_rev_parse)

    cloned = await manager._ensure_git_repository(manager._git_config())

    assert cloned is True
    assert clone_envs == [{"GIT_LFS_SKIP_SMUDGE": "1"}]
    assert ["lfs", "pull", "origin", "main"] in git_calls


@pytest.mark.asyncio
async def test_run_git_redacts_credentials_in_error_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git command errors should not leak embedded URL credentials."""
    manager = _git_manager(tmp_path)

    class _FailingProcess:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"",
                (
                    b"fatal: unable to access "
                    b"'https://x-access-token:secret-token@github.com/example/private.git/': "
                    b"The requested URL returned error: 403"
                ),
            )

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _FailingProcess:
        _ = args, kwargs
        return _FailingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match="Git command failed") as exc_info:
        await manager._run_git(
            [
                "clone",
                "https://x-access-token:secret-token@github.com/example/private.git",
                "dest",
            ],
        )

    message = str(exc_info.value)
    assert "secret-token" not in message
    assert "https://***@github.com/example/private.git" in message


@pytest.mark.asyncio
async def test_run_git_timeout_kills_subprocess_and_raises_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timed out git commands should terminate the child process and raise a redacted runtime error."""
    manager = _git_manager(tmp_path, sync_timeout_seconds=5)

    class _HangingProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.kill_called = False
            self.wait_called = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b"", b""

        def kill(self) -> None:
            self.kill_called = True

        async def wait(self) -> int:
            self.wait_called = True
            self.returncode = -9
            return -9

    process = _HangingProcess()

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _HangingProcess:
        _ = args, kwargs
        return process

    async def _fake_wait_for(awaitable: object, **kwargs: float) -> tuple[bytes, bytes]:
        _ = kwargs["timeout"]
        close = getattr(awaitable, "close", None)
        if callable(close):
            close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(asyncio, "wait_for", _fake_wait_for)
    monkeypatch.setattr(manager, "_git_sync_timeout_seconds", lambda: 1.0)

    with pytest.raises(RuntimeError, match=r"Git command timed out after 1s: git fetch origin main"):
        await manager._run_git(["fetch", "origin", "main"])

    assert process.kill_called is True
    assert process.wait_called is True


@pytest.mark.asyncio
async def test_run_git_preserves_index_lock_and_does_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git lock failures should surface immediately without deleting the lock file."""
    manager = _git_manager(tmp_path)
    repo_root = tmp_path / "repo"
    git_dir = repo_root / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    lock_path = git_dir / "index.lock"
    lock_path.write_text("", encoding="utf-8")

    class _FailingProcess:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"",
                (
                    f"fatal: Unable to create '{lock_path}': File exists.\n"
                    "Another git process seems to be running in this repository."
                ).encode(),
            )

    recorded_cwds: list[str] = []

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> object:
        _ = args
        recorded_cwds.append(str(kwargs["cwd"]))
        return _FailingProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    with pytest.raises(RuntimeError, match=r"index\.lock"):
        await manager._run_git(["checkout", "main"], cwd=repo_root)

    assert recorded_cwds == [str(repo_root)]
    assert lock_path.exists() is True


@pytest.mark.asyncio
async def test_run_git_cancellation_kills_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling a git command should terminate and reap the child process."""
    manager = _git_manager(tmp_path)
    wait_forever = asyncio.Event()

    class _HangingProcess:
        returncode: int | None = None

        def __init__(self) -> None:
            self.kill_called = False
            self.wait_called = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await wait_forever.wait()
            return b"", b""

        def kill(self) -> None:
            self.kill_called = True

        async def wait(self) -> int:
            self.wait_called = True
            self.returncode = -9
            return -9

    process = _HangingProcess()

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> _HangingProcess:
        _ = args, kwargs
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    task = asyncio.create_task(manager._run_git(["fetch", "origin", "main"]))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.kill_called is True
    assert process.wait_called is True


def test_redact_url_credentials_hides_entire_http_userinfo() -> None:
    """Knowledge Git URL redaction must not leak token usernames or URL parameters."""
    assert redact_url_credentials("https://user:password@example.com/repo.git") == "https://***@example.com/repo.git"
    assert redact_url_credentials("https://ghp_secret:x-oauth-basic@example.com/repo.git") == (
        "https://***@example.com/repo.git"
    )
    assert redact_url_credentials("https://username@example.com/repo.git") == "https://***@example.com/repo.git"
    assert redact_url_credentials("ssh://git@example.com/repo.git") == "ssh://***@example.com/repo.git"
    assert redact_url_credentials("ssh://user:pass@example.com/repo.git") == "ssh://***@example.com/repo.git"
    assert redact_url_credentials("git+https://user:pass@example.com/repo.git") == (
        "git+https://***@example.com/repo.git"
    )
    assert redact_url_credentials("https://example.com/repo.git?token=secret#frag-secret") == (
        "https://example.com/repo.git"
    )
    assert (
        redact_url_credentials("https://user:password@example.com/org/repo.git;token=secret?query=secret#frag-secret")
        == "https://***@example.com/org/repo.git"
    )
    assert (
        knowledge_manager_module._credential_free_repo_url(
            "https://user:password@example.com/repo.git?token=secret#frag-secret",
        )
        == "https://example.com/repo.git"
    )


def test_credential_free_repo_url_preserves_passwordless_ssh_username() -> None:
    """Passwordless SSH transport usernames are part of the clone identity."""
    assert (
        knowledge_manager_module._credential_free_repo_url(
            "ssh://git@example.com/org/repo.git;token=secret?query=secret#frag-secret",
        )
        == "ssh://git@example.com/org/repo.git"
    )


def test_credential_free_repo_url_strips_secret_bearing_userinfo() -> None:
    """Persistent clone URLs must not retain passwords, HTTP userinfo, query strings, or fragments."""
    assert (
        knowledge_manager_module._credential_free_repo_url(
            "ssh://git:secret@example.com/org/repo.git;token=secret?query=secret#frag-secret",
        )
        == "ssh://example.com/org/repo.git"
    )
    assert (
        knowledge_manager_module._credential_free_repo_url(
            "https://user@example.com/org/repo.git;token=secret?query=secret#frag-secret",
        )
        == "https://example.com/org/repo.git"
    )


def test_git_url_identity_preserves_passwordless_ssh_usernames() -> None:
    """Passwordless SSH usernames are identity, but secret-bearing userinfo is not."""
    assert credential_free_url_identity("ssh://git@example.com/org/repo.git") != credential_free_url_identity(
        "ssh://deploy@example.com/org/repo.git",
    )
    assert credential_free_url_identity("ssh://user:old@example.com/org/repo.git") == credential_free_url_identity(
        "ssh://user:new@example.com/org/repo.git",
    )
    assert credential_free_url_identity(
        "git+https://user:old@example.com/org/repo.git",
    ) == credential_free_url_identity("git+https://user:new@example.com/org/repo.git")
    assert credential_free_url_identity(
        "ssh://user:old@example.com/org/repo.git;token=secret?query=secret#frag-secret",
    ) == credential_free_url_identity("ssh://example.com/org/repo.git")
