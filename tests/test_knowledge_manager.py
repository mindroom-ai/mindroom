"""Knowledge snapshot and refresh behavior tests."""

from __future__ import annotations

import asyncio
import gc
import json
import os
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock

import pytest
from agno.knowledge.document.base import Document
from fastapi.testclient import TestClient

import mindroom.knowledge.manager as knowledge_manager_module
import mindroom.knowledge.refresh_runner as knowledge_refresh_runner
import mindroom.knowledge.registry as knowledge_registry
import mindroom.knowledge.utils as knowledge_utils
from mindroom.api import main
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentPrivateKnowledgeConfig
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.knowledge import (
    KnowledgeAvailability,
    KnowledgeAvailabilityDetail,
    PerBindingKnowledgeRefreshOwner,
    clear_published_snapshots,
    credential_free_url_identity,
    get_agent_knowledge,
    get_published_snapshot,
    knowledge_binding_mutation_lock,
    redact_url_credentials,
    refresh_knowledge_binding,
    snapshot_indexed_count,
)
from mindroom.knowledge.manager import KnowledgeManager, knowledge_source_signature, list_knowledge_files
from mindroom.knowledge.registry import load_published_indexing_state, resolve_snapshot_key, snapshot_metadata_path
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
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
    knowledge_registry._stale_ready_snapshots.clear()
    knowledge_refresh_runner._refresh_locks.clear()
    knowledge_refresh_runner._refresh_lock_accessed_at.clear()
    knowledge_refresh_runner._active_refresh_counts.clear()
    yield
    clear_published_snapshots()
    knowledge_utils._refresh_scheduled_at.clear()
    knowledge_registry._stale_ready_snapshots.clear()
    knowledge_refresh_runner._refresh_locks.clear()
    knowledge_refresh_runner._refresh_lock_accessed_at.clear()
    knowledge_refresh_runner._active_refresh_counts.clear()
    _VectorDb.collections = {}


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


def test_failed_notice_without_snapshot_says_unavailable() -> None:
    """Cold failed knowledge must not be described as stale when no snapshot is attached."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.REFRESH_FAILED,
                snapshot_attached=False,
            ),
        },
    )

    assert notice is not None
    assert "unavailable for semantic search this turn" in notice
    assert "may be stale" not in notice
    assert "Do not claim to have searched it." in notice


def test_config_mismatch_notice_without_snapshot_says_unavailable() -> None:
    """Cold config-mismatched knowledge must not imply stale semantic search occurred."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.CONFIG_MISMATCH,
                snapshot_attached=False,
            ),
        },
    )

    assert notice is not None
    assert "unavailable for semantic search this turn" in notice
    assert "may be stale" not in notice
    assert "Do not claim to have searched it." in notice


def test_stale_notice_without_snapshot_says_unavailable() -> None:
    """Stale metadata without a loadable snapshot must not imply semantic search occurred."""
    notice = knowledge_utils.format_knowledge_availability_notice(
        {
            "docs": KnowledgeAvailabilityDetail(
                availability=KnowledgeAvailability.STALE,
                snapshot_attached=False,
            ),
        },
    )

    assert notice is not None
    assert "unavailable for semantic search this turn" in notice
    assert "may be stale" not in notice
    assert "Do not claim to have searched it." in notice


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
async def test_shared_local_watch_snapshot_refreshes_on_access_without_blocking_read(tmp_path: Path) -> None:
    """Shared local bases with watch=true schedule refresh on access while serving last-good content."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("shared local old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("shared local new", encoding="utf-8")
    owner = PerBindingKnowledgeRefreshOwner()

    try:
        knowledge = get_agent_knowledge("helper", config, runtime_paths, refresh_owner=owner)
        assert knowledge is not None
        assert [document.content for document in knowledge.search("shared", max_results=5)] == ["shared local old"]

        for _attempt in range(50):
            await asyncio.sleep(0.01)
            refreshed = get_agent_knowledge("helper", config, runtime_paths)
            if refreshed is not None and [
                document.content for document in refreshed.search("shared", max_results=5)
            ] == ["shared local new"]:
                break
        else:
            pytest.fail("background on-access refresh did not publish the edited local source")
    finally:
        await owner.shutdown()


@pytest.mark.asyncio
async def test_shared_local_watch_ready_refresh_on_access_is_throttled(tmp_path: Path) -> None:
    """Local watch=true bases do not enqueue refresh work on every READY request."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("shared local old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"], watch=True)
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()

    assert get_agent_knowledge("helper", config, runtime_paths, refresh_owner=owner) is not None
    doc.write_text("shared local new", encoding="utf-8")
    assert get_agent_knowledge("helper", config, runtime_paths, refresh_owner=owner) is not None
    assert get_agent_knowledge("helper", config, runtime_paths, refresh_owner=owner) is not None

    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_stale_snapshot_metadata_schedules_refresh_without_source_scan(tmp_path: Path) -> None:
    """Ready access only uses persisted metadata/dirty markers, not request-time source scans."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("ready snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("ready snapshot changed", encoding="utf-8")
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = snapshot_metadata_path(key)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["availability"] = KnowledgeAvailability.STALE.value
    metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    clear_published_snapshots()
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
        refresh_owner=owner,
    )
    second_knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
        refresh_owner=owner,
    )

    assert knowledge is not None
    assert second_knowledge is not None
    assert [document.content for document in knowledge.search("snapshot", max_results=5)] == ["ready snapshot"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()
    assert owner.schedule_refresh.call_args.args == ("docs",)


@pytest.mark.asyncio
async def test_ready_snapshot_access_never_recomputes_source_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """READY request lookup must not walk the corpus to recompute source signatures."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("ready snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_indexing_state(snapshot_metadata_path(key))
    assert state is not None
    assert state.source_signature is not None

    def _unexpected_signature(*_args: object, **_kwargs: object) -> str:
        msg = "READY request lookup must not recompute knowledge source signatures"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.knowledge.manager.knowledge_source_signature", _unexpected_signature)

    assert get_agent_knowledge("helper", config, runtime_paths) is not None
    assert get_agent_knowledge("helper", config, runtime_paths) is not None


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
async def test_ready_legacy_snapshot_without_source_signature_is_reported_stale(tmp_path: Path) -> None:
    """Legacy READY metadata without a source signature schedules refresh and emits stale availability."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("legacy snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = snapshot_metadata_path(key)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload.pop("source_signature", None)
    metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    clear_published_snapshots()
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
        refresh_owner=owner,
    )

    assert knowledge is not None
    assert [document.content for document in knowledge.search("legacy", max_results=5)] == ["legacy snapshot"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_successful_publish_clears_stale_ready_markers(tmp_path: Path) -> None:
    """A stale marker for a reverted source signature should not survive a successful publish."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_indexing_state(snapshot_metadata_path(key))
    assert state is not None
    knowledge_registry._stale_ready_snapshots.add(
        (
            knowledge_registry.refresh_key_for_snapshot_key(key),
            state.source_signature,
        ),
    )

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    unavailable: dict[str, KnowledgeAvailability] = {}
    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
    )

    assert knowledge is not None
    assert unavailable == {}


def test_mark_stale_uses_default_collection_for_legacy_metadata_without_collection(tmp_path: Path) -> None:
    """Legacy metadata without a collection field can still be marked stale without mutating vectors."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("legacy delete", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    default_collection = manager._default_collection_name()
    _VectorDb.collections[default_collection] = [
        {"content": "legacy delete", "metadata": {"source_path": "guide.md"}},
    ]
    metadata_path = snapshot_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(list(key.indexing_settings)), encoding="utf-8")

    marked_base_ids = knowledge_registry.mark_published_snapshot_stale(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
    )
    state = load_published_indexing_state(metadata_path)

    assert marked_base_ids == ("docs",)
    assert _VectorDb.collections[default_collection] == [
        {"content": "legacy delete", "metadata": {"source_path": "guide.md"}},
    ]
    assert state is not None
    assert state.availability == KnowledgeAvailability.STALE.value


@pytest.mark.asyncio
async def test_mark_stale_fans_out_to_duplicate_physical_sources(tmp_path: Path) -> None:
    """Mutating one base should stale every published snapshot that reads the same source folder."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "guide.md"
    doc.write_text("shared source old", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("alpha", config=config, runtime_paths=runtime_paths)
    await refresh_knowledge_binding("beta", config=config, runtime_paths=runtime_paths)
    beta_lookup = get_published_snapshot("beta", config=config, runtime_paths=runtime_paths)
    assert beta_lookup.snapshot is not None
    assert beta_lookup.availability is KnowledgeAvailability.READY
    doc.write_text("shared source new", encoding="utf-8")

    marked_base_ids = knowledge_registry.mark_published_snapshot_stale(
        "alpha",
        config=config,
        runtime_paths=runtime_paths,
    )
    beta_key = resolve_snapshot_key("beta", config=config, runtime_paths=runtime_paths)
    beta_state = load_published_indexing_state(snapshot_metadata_path(beta_key))
    refreshed_beta_lookup = get_published_snapshot("beta", config=config, runtime_paths=runtime_paths)

    assert marked_base_ids == ("alpha", "beta")
    assert beta_state is not None
    assert beta_state.availability == KnowledgeAvailability.STALE.value
    assert refreshed_beta_lookup.availability is KnowledgeAvailability.STALE


def test_legacy_git_raw_url_metadata_is_compatible_with_redacted_identity(tmp_path: Path) -> None:
    """Old metadata that stored raw Git URLs should remain compatible with credential-free identities."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("legacy git", encoding="utf-8")
    raw_repo_url = "https://token:secret@example.com/org/repo.git"
    git_config = KnowledgeGitConfig(repo_url=raw_repo_url)
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    legacy_settings = list(key.indexing_settings)
    legacy_settings[9] = raw_repo_url
    collection = "legacy_git_collection"
    _VectorDb.collections[collection] = [
        {"content": "legacy git", "metadata": {"source_path": "doc.md"}},
    ]
    metadata_path = snapshot_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "settings": legacy_settings,
                "status": "complete",
                "collection": collection,
                "availability": "ready",
                "source_signature": "legacy-signature",
            },
        ),
        encoding="utf-8",
    )

    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert lookup.snapshot is not None
    assert lookup.availability is KnowledgeAvailability.READY
    assert [document.content for document in lookup.snapshot.knowledge.search("git", max_results=5)] == ["legacy git"]


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
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)

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
async def test_git_ready_snapshot_schedules_refresh_after_poll_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git READY access polls in the background even when the local checkout signature is unchanged."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git snapshot", encoding="utf-8")
    git_config = KnowledgeGitConfig(repo_url="https://example.com/org/repo.git", poll_interval_seconds=5)
    config = _config(
        tmp_path,
        bases={"docs": docs_path},
        agent_bases=["docs"],
        git_configs={"docs": git_config},
    )
    runtime_paths = runtime_paths_for(config)

    async def _sync_success(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        _ = index_changes
        self._git_last_successful_commit = "rev-a"
        _set_git_tracked_files(self, "doc.md")
        return {"updated": False, "changed_count": 0, "removed_count": 0}

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync_success)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = snapshot_metadata_path(key)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["last_published_at"] = "2000-01-01T00:00:00+00:00"
    metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    clear_published_snapshots()
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    def _unexpected_signature(*_args: object, **_kwargs: object) -> str:
        msg = "git ready access should not scan the local corpus"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.knowledge.manager.knowledge_source_signature", _unexpected_signature)

    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
        refresh_owner=owner,
    )

    assert knowledge is not None
    assert [document.content for document in knowledge.search("git", max_results=5)] == ["git snapshot"]
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_private_git_ready_refresh_on_access_honors_poll_interval(
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
    key = resolve_snapshot_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity,
        create=True,
    )
    knowledge_path = Path(key.knowledge_path)
    knowledge_path.mkdir(parents=True, exist_ok=True)
    (knowledge_path / "note.md").write_text("alice private git note", encoding="utf-8")

    async def _sync_success(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        _ = index_changes
        self._git_last_successful_commit = "rev-a"
        _set_git_tracked_files(self, "note.md")
        return {"updated": False, "changed_count": 0, "removed_count": 0}

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync_success)
    await refresh_knowledge_binding(base_id, config=config, runtime_paths=runtime_paths, execution_identity=identity)
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity,
        refresh_owner=owner,
        on_unavailable_bases=unavailable.update,
    )

    assert knowledge is not None
    assert unavailable == {}
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_not_called()

    metadata_path = snapshot_metadata_path(key)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["last_published_at"] = "2000-01-01T00:00:00+00:00"
    metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    clear_published_snapshots()
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable = {}

    stale_knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity,
        refresh_owner=owner,
        on_unavailable_bases=unavailable.update,
    )

    assert stale_knowledge is not None
    assert unavailable == {base_id: KnowledgeAvailability.STALE}
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()


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
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
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
async def test_refresh_discards_candidate_when_sources_change_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Published metadata stays bound to the exact corpus that was indexed."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("stable snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("candidate snapshot", encoding="utf-8")
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
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is False
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert result.last_error == "Knowledge source changed during refresh; refresh skipped"
    assert lookup.snapshot is not None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED
    assert [document.content for document in lookup.snapshot.knowledge.search("snapshot", max_results=5)] == [
        "stable snapshot",
    ]


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

    async def _blocked_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        _ = protected_collections
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
    doc.write_text("snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"alpha": docs_path, "beta": docs_path}, agent_bases=["alpha", "beta"])
    runtime_paths = runtime_paths_for(config)
    refresh_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    mutation_entered = asyncio.Event()

    async def _blocked_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        _ = (self, protected_collections)
        refresh_entered.set()
        await release_refresh.wait()
        return 0

    async def _mutate_shared_source() -> None:
        async with knowledge_binding_mutation_lock("beta", config=config, runtime_paths=runtime_paths):
            mutation_entered.set()
            doc.write_text("mutated", encoding="utf-8")
            knowledge_registry.mark_published_snapshot_stale(
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
    del first_lookup
    gc.collect()

    for generation in range(2, 7):
        doc.write_text(f"generation {generation}", encoding="utf-8")
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert first_collection in _VectorDb.collections
    assert [document.content for document in first_knowledge.search("generation", max_results=5)] == ["generation one"]
    latest = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert latest.snapshot is not None
    assert [document.content for document in latest.snapshot.knowledge.search("generation", max_results=5)] == [
        "generation 6",
    ]


@pytest.mark.asyncio
async def test_publish_invalidates_cached_snapshots_for_same_physical_binding(tmp_path: Path) -> None:
    """A config transition and revert must not resurrect an older cached handle for the same path."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("config a", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    cached_a = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert cached_a.snapshot is not None
    assert [document.content for document in cached_a.snapshot.knowledge.search("config", max_results=5)] == [
        "config a",
    ]

    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    doc.write_text("config b", encoding="utf-8")
    await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)
    reverted_lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert reverted_lookup.snapshot is not None
    assert reverted_lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH
    assert [document.content for document in reverted_lookup.snapshot.knowledge.search("config", max_results=5)] == [
        "config b",
    ]


@pytest.mark.asyncio
async def test_successful_refreshes_keep_bounded_retention_metadata(tmp_path: Path) -> None:
    """Repeated publishes bound retained metadata without deleting live read-handle collections."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("generation 0", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    for generation in range(6):
        doc.write_text(f"generation {generation}", encoding="utf-8")
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_indexing_state(snapshot_metadata_path(key))
    assert state is not None
    assert len(state.retained_collections) <= 3
    assert len(_VectorDb.collections) <= 3
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.snapshot is not None
    assert [document.content for document in lookup.snapshot.knowledge.search("generation", max_results=5)] == [
        "generation 5",
    ]


@pytest.mark.asyncio
async def test_publish_cleans_legacy_default_collection_after_migration(tmp_path: Path) -> None:
    """A migrated metadata record without collection should not leave the old default collection forever."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("legacy default old", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    manager = KnowledgeManager("docs", config=config, runtime_paths=runtime_paths)
    default_collection = manager._default_collection_name()
    _VectorDb.collections[default_collection] = [
        {"content": "legacy default old", "metadata": {"source_path": "doc.md"}},
    ]
    metadata_path = snapshot_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(list(key.indexing_settings)), encoding="utf-8")
    doc.write_text("legacy default new", encoding="utf-8")

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert default_collection not in _VectorDb.collections
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.snapshot is not None
    assert [document.content for document in lookup.snapshot.knowledge.search("legacy", max_results=5)] == [
        "legacy default new",
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
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is True
    assert result.availability is KnowledgeAvailability.READY
    assert lookup.snapshot is not None
    assert lookup.availability is KnowledgeAvailability.READY
    assert [document.content for document in lookup.snapshot.knowledge.search("cleanup", max_results=5)] == [
        "cleanup new",
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
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
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
async def test_metadata_save_failure_after_candidate_index_keeps_serving_last_good(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate whose metadata did not commit must not replace the published read handle."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    doc = docs_path / "doc.md"
    doc.write_text("stable metadata snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("uncommitted candidate snapshot", encoding="utf-8")
    original_save = KnowledgeManager._save_persisted_indexing_state

    def _fail_candidate_metadata_save(
        self: KnowledgeManager,
        status: object,
        **kwargs: object,
    ) -> None:
        if kwargs.get("availability") == "ready" and "_candidate_" in str(kwargs.get("collection")):
            msg = "metadata commit failed"
            raise OSError(msg)
        original_save(self, status, **kwargs)

    monkeypatch.setattr(KnowledgeManager, "_save_persisted_indexing_state", _fail_candidate_metadata_save)
    with pytest.raises(OSError, match="metadata commit failed"):
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
    assert [document.content for document in knowledge.search("snapshot", max_results=5)] == [
        "stable metadata snapshot",
    ]


@pytest.mark.asyncio
async def test_partial_refresh_after_cached_snapshot_updates_failed_availability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial refresh must not leave the process-local READY snapshot hiding failure metadata."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "good.md").write_text("last good", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    assert get_agent_knowledge("helper", config, runtime_paths) is not None
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
    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
    )

    assert result.published is False
    assert result.availability is KnowledgeAvailability.REFRESH_FAILED
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert knowledge is not None
    assert [document.content for document in knowledge.search("good", max_results=5)] == ["last good"]


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
async def test_config_mismatch_refresh_cooldown_is_settings_aware(tmp_path: Path) -> None:
    """A newer config mismatch for the same binding must not be dropped by the request cooldown."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()

    assert get_agent_knowledge("helper", changed_config, runtime_paths, refresh_owner=owner) is not None
    assert get_agent_knowledge("helper", newer_config, runtime_paths, refresh_owner=owner) is not None

    assert owner.schedule_refresh.call_count == 2
    assert owner.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert owner.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


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
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()

    assert get_agent_knowledge("helper", changed_config, runtime_paths, refresh_owner=owner) is None
    assert get_agent_knowledge("helper", newer_config, runtime_paths, refresh_owner=owner) is None

    assert owner.schedule_initial_load.call_count == 2
    assert owner.schedule_initial_load.call_args_list[0].kwargs["config"] is changed_config
    assert owner.schedule_initial_load.call_args_list[1].kwargs["config"] is newer_config
    owner.schedule_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_cold_failed_refresh_cooldown_is_settings_aware(tmp_path: Path) -> None:
    """A failed cold refresh under old settings must not suppress a newer config's retry."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = snapshot_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(
        json.dumps(
            {
                "settings": list(key.indexing_settings),
                "status": "indexing",
                "availability": KnowledgeAvailability.REFRESH_FAILED.value,
                "last_error": "cold failure",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    assert (
        get_agent_knowledge(
            "helper",
            changed_config,
            runtime_paths,
            refresh_owner=owner,
            on_unavailable_bases=unavailable.update,
        )
        is None
    )
    assert (
        get_agent_knowledge(
            "helper",
            newer_config,
            runtime_paths,
            refresh_owner=owner,
            on_unavailable_bases=unavailable.update,
        )
        is None
    )

    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    assert owner.schedule_refresh.call_count == 2
    assert owner.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert owner.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


@pytest.mark.asyncio
@pytest.mark.parametrize("availability", [KnowledgeAvailability.STALE, KnowledgeAvailability.REFRESH_FAILED])
async def test_stale_or_failed_snapshot_reports_chunking_config_mismatch_before_cooldown(
    tmp_path: Path,
    availability: KnowledgeAvailability,
) -> None:
    """Stale/failed metadata must not suppress refreshes for newer chunking settings."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("old snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    metadata_path = snapshot_metadata_path(key)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["availability"] = availability.value
    if availability is KnowledgeAvailability.REFRESH_FAILED:
        payload["last_error"] = "previous failure"
    metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    clear_published_snapshots()
    changed_config = config.model_copy(deep=True)
    changed_config.knowledge_bases["docs"].chunk_size = 1024
    newer_config = config.model_copy(deep=True)
    newer_config.knowledge_bases["docs"].chunk_size = 2048
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    assert (
        get_agent_knowledge(
            "helper",
            changed_config,
            runtime_paths,
            refresh_owner=owner,
            on_unavailable_bases=unavailable.update,
        )
        is not None
    )
    assert (
        get_agent_knowledge(
            "helper",
            newer_config,
            runtime_paths,
            refresh_owner=owner,
            on_unavailable_bases=unavailable.update,
        )
        is not None
    )

    assert unavailable == {"docs": KnowledgeAvailability.CONFIG_MISMATCH}
    assert owner.schedule_refresh.call_count == 2
    assert owner.schedule_refresh.call_args_list[0].kwargs["config"] is changed_config
    assert owner.schedule_refresh.call_args_list[1].kwargs["config"] is newer_config


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
        _set_git_tracked_files(self, "doc.md")
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
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
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
    assert lookup.availability is KnowledgeAvailability.CONFIG_MISMATCH


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


def test_lookup_failure_after_binding_resolution_schedules_repair_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolved binding with a broken read handle should still queue a repair refresh."""
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
                "collection": "broken_collection",
                "availability": "ready",
            },
        ),
        encoding="utf-8",
    )
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    def _broken_vector_db(*_args: object, **_kwargs: object) -> object:
        msg = "cannot open collection"
        raise RuntimeError(msg)

    monkeypatch.setattr("mindroom.knowledge.registry._build_snapshot_vector_db", _broken_vector_db)

    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
        refresh_owner=owner,
    )

    assert knowledge is None
    assert unavailable == {"docs": KnowledgeAvailability.REFRESH_FAILED}
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()


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
        indexed_signatures: dict[str, tuple[int, int, str] | None] | None = None,
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

    async def _raise_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        _ = (self, protected_collections)
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
async def test_refresh_setup_failure_records_failed_availability(tmp_path: Path) -> None:
    """Manager construction failures are persisted instead of leaving cold metadata initializing."""
    docs_path = tmp_path / "docs"
    docs_path.write_text("not a directory", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)

    with pytest.raises(ValueError, match="must be a directory"):
        await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_indexing_state(snapshot_metadata_path(key))
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert state is not None
    assert state.availability == KnowledgeAvailability.REFRESH_FAILED.value
    assert state.last_error is not None
    assert "must be a directory" in state.last_error
    assert lookup.snapshot is None
    assert lookup.availability is KnowledgeAvailability.REFRESH_FAILED


@pytest.mark.asyncio
async def test_api_delete_marks_snapshot_stale_without_mutating_published_vectors(tmp_path: Path) -> None:
    """DELETE success schedules refresh while retained published collections stay immutable."""
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
    unavailable: dict[str, KnowledgeAvailability] = {}
    after_delete = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
    )

    assert response.status_code == 200
    assert after_delete is not None
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    assert [document.content for document in after_delete.search("delete", max_results=5)] == ["delete me now"]
    owner.schedule_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_api_upload_marks_snapshot_stale_without_mutating_published_vectors(tmp_path: Path) -> None:
    """Upload mutations should not edit the retained last-good published collection."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("old upload", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    owner = MagicMock()
    owner.schedule_refresh = MagicMock()
    main.app.state.knowledge_refresh_owner = owner
    client = TestClient(main.app)

    response = client.post(
        "/api/knowledge/bases/docs/upload",
        files=[("files", ("guide.md", b"new upload", "text/markdown"))],
    )
    unavailable: dict[str, KnowledgeAvailability] = {}
    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        on_unavailable_bases=unavailable.update,
    )

    assert response.status_code == 200
    assert knowledge is not None
    assert unavailable == {"docs": KnowledgeAvailability.STALE}
    assert [document.content for document in knowledge.search("upload", max_results=5)] == ["old upload"]
    owner.schedule_refresh.assert_called_once()


def test_api_upload_failure_preserves_existing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A later upload failure must not leave earlier replacements committed."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("existing upload", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    owner = MagicMock()
    owner.schedule_refresh = MagicMock()
    main.app.state.knowledge_refresh_owner = owner
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
    assert (docs_path / "guide.md").read_text(encoding="utf-8") == "existing upload"
    assert not (docs_path / "new.md").exists()
    owner.schedule_refresh.assert_not_called()


@pytest.mark.asyncio
async def test_api_status_reports_direct_refresh_runner_reindex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Status polling should see explicit refresh_knowledge_binding calls, not only owner-scheduled jobs."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "guide.md").write_text("refreshing status", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    main.initialize_api_app(main.app, runtime_paths)
    _publish_api_config(main.app, config)
    main.app.state.knowledge_refresh_owner = None
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        _ = (self, protected_collections)
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
async def test_refresh_owner_done_task_keeps_newest_pending_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed task still owned by its callback must not let an older pending request run."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    old_config = config.model_copy(deep=True)
    old_config.knowledge_bases["docs"].chunk_size = 2048
    newest_config = config.model_copy(deep=True)
    newest_config.knowledge_bases["docs"].chunk_size = 4096
    runtime_paths = runtime_paths_for(config)
    owner = PerBindingKnowledgeRefreshOwner()
    key = knowledge_registry.resolve_refresh_key("docs", config=config, runtime_paths=runtime_paths)
    seen_chunk_sizes: list[int] = []

    async def _completed_refresh() -> None:
        return None

    async def _fake_refresh(base_id: str, **kwargs: object) -> object:
        _ = base_id
        refresh_config = kwargs["config"]
        assert isinstance(refresh_config, Config)
        seen_chunk_sizes.append(refresh_config.knowledge_bases["docs"].chunk_size)
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_owner.refresh_knowledge_binding", _fake_refresh)
    done_task = asyncio.create_task(_completed_refresh())
    await asyncio.sleep(0)
    assert done_task.done()
    owner._tasks[key] = done_task

    owner.schedule_refresh("docs", config=old_config, runtime_paths=runtime_paths)
    owner.schedule_refresh("docs", config=newest_config, runtime_paths=runtime_paths)
    owner._handle_done(key, done_task)
    await asyncio.sleep(0)
    await owner.shutdown()

    assert seen_chunk_sizes == [4096]


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


@pytest.mark.asyncio
async def test_refresh_status_is_visible_across_owner_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard status owners should see refreshes started by the Matrix/orchestrator owner."""
    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    matrix_owner = PerBindingKnowledgeRefreshOwner()
    api_owner = PerBindingKnowledgeRefreshOwner()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked_refresh(base_id: str, **_kwargs: object) -> object:
        _ = base_id
        started.set()
        await release.wait()
        return object()

    monkeypatch.setattr("mindroom.knowledge.refresh_owner.refresh_knowledge_binding", _blocked_refresh)

    matrix_owner.schedule_refresh("docs", config=config, runtime_paths=runtime_paths)
    await started.wait()

    try:
        assert api_owner.is_refreshing("docs", config=config, runtime_paths=runtime_paths) is True
    finally:
        release.set()
        await matrix_owner.shutdown()
        await api_owner.shutdown()


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
async def test_private_request_scoped_knowledge_publishes_isolated_snapshots(tmp_path: Path) -> None:
    """Requester-local private knowledge must resolve to separate physical snapshot bindings."""
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
    key_a = resolve_snapshot_key(
        base_id,
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=identity_a,
        create=True,
    )
    key_b = resolve_snapshot_key(
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
    knowledge_a = get_agent_knowledge("helper", config, runtime_paths, execution_identity=identity_a)
    knowledge_b = get_agent_knowledge("helper", config, runtime_paths, execution_identity=identity_b)

    assert key_a != key_b
    assert knowledge_a is not None
    assert knowledge_b is not None
    assert [document.content for document in knowledge_a.search("private", max_results=5)] == ["alice private note"]
    assert [document.content for document in knowledge_b.search("private", max_results=5)] == ["bob private note"]


@pytest.mark.asyncio
async def test_private_request_scoped_knowledge_schedules_refresh_when_source_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requester-local READY snapshots should be served and refreshed without request-time scans."""
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
    key = resolve_snapshot_key(
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
    owner = MagicMock()
    owner.schedule_initial_load = MagicMock()
    owner.schedule_refresh = MagicMock()
    unavailable: dict[str, KnowledgeAvailability] = {}

    def _unexpected_signature(*_args: object, **_kwargs: object) -> str:
        msg = "private READY access should not scan the local corpus"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.knowledge.manager.knowledge_source_signature", _unexpected_signature)
    monkeypatch.setattr(knowledge_utils, "knowledge_source_signature", _unexpected_signature, raising=False)

    knowledge = get_agent_knowledge(
        "helper",
        config,
        runtime_paths,
        execution_identity=identity,
        refresh_owner=owner,
        on_unavailable_bases=unavailable.update,
    )

    assert knowledge is not None
    assert [document.content for document in knowledge.search("private", max_results=5)] == ["alice private old"]
    assert unavailable == {}
    owner.schedule_initial_load.assert_not_called()
    owner.schedule_refresh.assert_called_once()


def test_private_request_scoped_bookkeeping_is_bounded(tmp_path: Path) -> None:
    """Private snapshot, lock, and refresh-cooldown registries should be pruned."""
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
        knowledge_registry._MAX_PRIVATE_PUBLISHED_SNAPSHOTS,
        knowledge_utils._MAX_REFRESH_SCHEDULED_COOLDOWNS,
        knowledge_refresh_runner._MAX_REFRESH_LOCKS,
    )

    for index in range(max_entries + 40):
        identity = _identity(f"@user{index}:localhost")
        key = resolve_snapshot_key(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
            create=True,
        )
        collection = f"private_collection_{index}"
        refresh_key = knowledge_registry.refresh_key_for_snapshot_key(key)
        knowledge_registry.publish_snapshot(
            key,
            knowledge=_Knowledge(_VectorDb(collection=collection)),
            state=knowledge_registry.PublishedIndexingState(
                settings=key.indexing_settings,
                status="complete",
                collection=collection,
                availability="ready",
                source_signature=f"sig-{index}",
            ),
            metadata_path=snapshot_metadata_path(key),
        )
        knowledge_utils._refresh_schedule_due(
            refresh_key,
            KnowledgeAvailability.READY,
            settings=key.indexing_settings,
            cooldown_seconds=300,
        )
        knowledge_refresh_runner._refresh_lock_for_key(refresh_key)

    private_snapshot_count = sum(
        key.base_id.startswith(config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX)
        for key in knowledge_registry._published_snapshots
    )
    assert private_snapshot_count <= knowledge_registry._MAX_PRIVATE_PUBLISHED_SNAPSHOTS
    assert len(knowledge_utils._refresh_scheduled_at) <= knowledge_utils._MAX_REFRESH_SCHEDULED_COOLDOWNS
    assert len(knowledge_refresh_runner._refresh_locks) <= knowledge_refresh_runner._MAX_REFRESH_LOCKS


def test_non_weakrefable_snapshot_handle_does_not_leak_collection_lease(tmp_path: Path) -> None:
    """If weakref.finalize cannot protect a handle, its provisional lease is removed."""

    class _NonWeakrefKnowledge:
        __slots__ = ()

    docs_path = tmp_path / "docs"
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    knowledge = _NonWeakrefKnowledge()

    knowledge_registry.publish_snapshot(
        key,
        knowledge=knowledge,
        state=knowledge_registry.PublishedIndexingState(
            settings=key.indexing_settings,
            status="complete",
            collection="non_weakref_collection",
            availability="ready",
        ),
        metadata_path=snapshot_metadata_path(key),
    )

    assert id(knowledge) not in knowledge_registry._published_knowledge_leases


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
async def test_local_noop_refresh_reports_published_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchanged local refresh republishes a usable snapshot and reports it as published."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("local snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _track_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        nonlocal reindex_count
        reindex_count += 1
        if reindex_count > 1:
            msg = "unchanged local refresh should not reindex"
            raise AssertionError(msg)
        return await original_reindex(self, protected_collections=protected_collections)

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is True
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
    doc.write_text("old snapshot", encoding="utf-8")
    initial_stat = doc.stat()
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    reindex_count = 0
    original_reindex = KnowledgeManager.reindex_all

    async def _track_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self, protected_collections=protected_collections)

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("new snapshot", encoding="utf-8")
    os.utime(doc, ns=(initial_stat.st_atime_ns, initial_stat.st_mtime_ns))
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is True
    assert reindex_count == 2
    assert lookup.snapshot is not None
    assert [document.content for document in lookup.snapshot.knowledge.search("snapshot", max_results=5)] == [
        "new snapshot",
    ]


@pytest.mark.asyncio
async def test_refresh_rebuilds_missing_metadata_with_source_signature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful refresh must not publish synthesized READY metadata without a source signature."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("metadata snapshot", encoding="utf-8")
    config = _config(tmp_path, bases={"docs": docs_path}, agent_bases=["docs"])
    runtime_paths = runtime_paths_for(config)
    original_reindex = KnowledgeManager.reindex_all

    async def _delete_metadata_after_reindex(
        self: KnowledgeManager,
        *,
        protected_collections: tuple[str, ...] = (),
    ) -> int:
        indexed_count = await original_reindex(self, protected_collections=protected_collections)
        self._indexing_settings_path.unlink()
        return indexed_count

    monkeypatch.setattr(KnowledgeManager, "reindex_all", _delete_metadata_after_reindex)

    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_indexing_state(snapshot_metadata_path(key))

    assert result.published is True
    assert state is not None
    assert state.availability == KnowledgeAvailability.READY.value
    assert state.source_signature == knowledge_source_signature(config, "docs", docs_path)


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
        knowledge_registry.save_published_indexing_state(
            metadata_path,
            knowledge_registry.PublishedIndexingState(
                settings=("settings",),
                status="complete",
                collection="collection",
                availability="ready",
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
        _set_git_tracked_files(self, "doc.md")
        return {"updated": True, "changed_count": 1, "removed_count": 0}

    async def _track_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        order.append("reindex")
        return await original_reindex(self, protected_collections=protected_collections)

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
    assert state.source_signature == knowledge_source_signature(
        config,
        "docs",
        docs_path,
        tracked_relative_paths={"doc.md"},
    )
    assert "ghp_secret" not in metadata_text
    assert "x-oauth-basic" not in metadata_text


@pytest.mark.asyncio
async def test_git_noop_refresh_skips_full_reindex_when_snapshot_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged Git poll should update sync metadata without rebuilding the collection."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git snapshot", encoding="utf-8")
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

    async def _sync(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        assert index_changes is False
        result = sync_results.pop(0)
        self._git_last_successful_commit = str(result["commit"])
        _set_git_tracked_files(self, "doc.md")
        return result

    async def _track_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        nonlocal reindex_count
        reindex_count += 1
        if reindex_count > 1:
            msg = "unchanged git poll should not reindex"
            raise AssertionError(msg)
        return await original_reindex(self, protected_collections=protected_collections)

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is True
    assert result.indexed_count == 1
    assert reindex_count == 1


@pytest.mark.asyncio
async def test_git_noop_refresh_ignores_untracked_indexable_file_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Git-backed corpora use tracked files only and ignore untracked checkout files."""
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    (docs_path / "doc.md").write_text("git tracked snapshot", encoding="utf-8")
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

    async def _sync(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        assert index_changes is False
        result = sync_results.pop(0)
        self._git_last_successful_commit = str(result["commit"])
        _set_git_tracked_files(self, "doc.md")
        return result

    async def _track_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self, protected_collections=protected_collections)

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    (docs_path / "untracked.md").write_text("git untracked local corpus", encoding="utf-8")
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is True
    assert reindex_count == 1
    assert lookup.snapshot is not None
    assert [document.content for document in lookup.snapshot.knowledge.search("git", max_results=5)] == [
        "git tracked snapshot",
    ]


@pytest.mark.asyncio
async def test_git_noop_refresh_rebuilds_when_collection_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged Git poll must repair metadata that points at a missing collection."""
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

    async def _sync(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        assert index_changes is False
        result = sync_results.pop(0)
        self._git_last_successful_commit = str(result["commit"])
        _set_git_tracked_files(self, "doc.md")
        return result

    async def _track_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self, protected_collections=protected_collections)

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    key = resolve_snapshot_key("docs", config=config, runtime_paths=runtime_paths)
    state = load_published_indexing_state(snapshot_metadata_path(key))
    assert state is not None
    assert state.collection is not None
    _VectorDb.collections.pop(state.collection, None)
    result = await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)

    assert result.published is True
    assert reindex_count == 2
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.snapshot is not None
    assert [document.content for document in lookup.snapshot.knowledge.search("git", max_results=5)] == [
        "git repaired",
    ]


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

    async def _sync(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        assert index_changes is False
        result = sync_results.pop(0)
        self._git_last_successful_commit = str(result["commit"])
        _set_git_tracked_files(self, "doc.md")
        return result

    async def _track_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self, protected_collections=protected_collections)

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("git chunking rebuilt", encoding="utf-8")
    result = await refresh_knowledge_binding("docs", config=changed_config, runtime_paths=runtime_paths)

    assert result.published is True
    assert reindex_count == 2
    lookup = get_published_snapshot("docs", config=changed_config, runtime_paths=runtime_paths)
    assert lookup.snapshot is not None
    assert [document.content for document in lookup.snapshot.knowledge.search("git", max_results=5)] == [
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

    async def _sync(self: KnowledgeManager, *, index_changes: bool = True) -> dict[str, object]:
        assert index_changes is False
        result = sync_results.pop(0)
        self._git_last_successful_commit = str(result["commit"])
        _set_git_tracked_files(self, "doc.md")
        return result

    async def _track_reindex(self: KnowledgeManager, *, protected_collections: tuple[str, ...] = ()) -> int:
        nonlocal reindex_count
        reindex_count += 1
        return await original_reindex(self, protected_collections=protected_collections)

    monkeypatch.setattr(KnowledgeManager, "sync_git_repository", _sync)
    monkeypatch.setattr(KnowledgeManager, "reindex_all", _track_reindex)

    await refresh_knowledge_binding("docs", config=config, runtime_paths=runtime_paths)
    doc.write_text("git force rebuilt", encoding="utf-8")
    result = await refresh_knowledge_binding(
        "docs",
        config=config,
        runtime_paths=runtime_paths,
        force_reindex=True,
    )

    assert result.published is True
    assert reindex_count == 2
    lookup = get_published_snapshot("docs", config=config, runtime_paths=runtime_paths)
    assert lookup.snapshot is not None
    assert [document.content for document in lookup.snapshot.knowledge.search("git", max_results=5)] == [
        "git force rebuilt",
    ]


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
        _set_git_tracked_files(self, "doc.md")
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
    assert redact_url_credentials("ssh://git@example.com/repo.git") == "ssh://***@example.com/repo.git"
    assert redact_url_credentials("ssh://user:pass@example.com/repo.git") == "ssh://***@example.com/repo.git"
    assert redact_url_credentials("git+https://user:pass@example.com/repo.git") == (
        "git+https://***@example.com/repo.git"
    )


def test_git_url_identity_strips_non_http_userinfo() -> None:
    """Credential rotation in parsed Git URLs must not change the persisted repo identity."""
    assert credential_free_url_identity("ssh://user:old@example.com/org/repo.git") == credential_free_url_identity(
        "ssh://user:new@example.com/org/repo.git",
    )
    assert credential_free_url_identity(
        "git+https://user:old@example.com/org/repo.git",
    ) == credential_free_url_identity("git+https://user:new@example.com/org/repo.git")
