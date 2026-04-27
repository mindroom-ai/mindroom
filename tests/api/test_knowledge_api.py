"""Tests for non-initializing knowledge management API routes."""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from contextlib import suppress
from io import BytesIO
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile
from starlette.requests import Request

import mindroom.knowledge.registry as knowledge_registry
from mindroom import constants
from mindroom.api import knowledge as knowledge_api
from mindroom.api import main
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.knowledge import KnowledgeAvailability
from mindroom.knowledge.registry import (
    load_published_index_state,
    published_index_metadata_path,
    resolve_published_index_key,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _knowledge_config(
    path: Path,
    *,
    extra_base: bool = False,
    duplicate_source_base: bool = False,
    git: bool = False,
) -> Config:
    knowledge_bases = {
        "research": KnowledgeBaseConfig(
            path=str(path),
            watch=False,
            git=KnowledgeGitConfig(repo_url="https://example.com/org/research.git") if git else None,
        ),
    }
    if extra_base:
        knowledge_bases["unused"] = KnowledgeBaseConfig(path=str(path.parent / "unused"), watch=False)
    if duplicate_source_base:
        knowledge_bases["summary"] = KnowledgeBaseConfig(path=str(path), watch=False, chunk_size=1024)
    return Config(agents={}, models={}, knowledge_bases=knowledge_bases)


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


def _publish_committed_runtime_config(api_app: object, config: Config) -> None:
    context = main._app_context(api_app)
    context.config_data = config.authored_model_dump()
    context.runtime_config = config
    context.config_load_result = main.ConfigLoadResult(success=True)


def _write_index_metadata(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    base_id: str = "research",
    collection: str = "published_collection",
    revision: str | None = None,
    published_at: str | None = None,
    last_error: str | None = None,
    indexed_count: int | None = None,
) -> None:
    key = resolve_published_index_key(base_id, config=config, runtime_paths=runtime_paths)
    metadata_path = published_index_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "settings": list(key.indexing_settings),
        "status": "complete",
        "collection": collection,
        "indexed_count": 0 if indexed_count is None else indexed_count,
        "source_signature": "test-source-signature",
    }
    if revision is not None:
        payload["published_revision"] = revision
    if published_at is not None:
        payload["last_published_at"] = published_at
    metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    if last_error is None:
        knowledge_registry.mark_published_index_refresh_succeeded(key)
    else:
        knowledge_registry.mark_published_index_refresh_failed_preserving_last_good(key, error=last_error)


def _init_git_checkout(path: Path, *tracked_paths: str) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    if tracked_paths:
        subprocess.run(["git", "add", *tracked_paths], cwd=path, check=True, capture_output=True)


def _test_client(tmp_path: Path) -> TestClient:
    runtime_paths = _runtime_paths(tmp_path)
    main.initialize_api_app(main.app, runtime_paths)
    main.app.state.knowledge_refresh_scheduler = None
    return TestClient(main.app)


class _RecordingRefreshScheduler:
    def __init__(self) -> None:
        self.scheduled: list[tuple[str, Config, RuntimePaths]] = []

    def schedule_refresh(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: object | None = None,
    ) -> None:
        _ = execution_identity
        self.scheduled.append((base_id, config, runtime_paths))

    def is_refreshing(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: object | None = None,
    ) -> bool:
        _ = (base_id, config, runtime_paths, execution_identity)
        return False


def test_knowledge_status_reads_index_metadata_without_initializing(tmp_path: Path) -> None:
    """Status for a cold base should read files only and avoid refresh/index work."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["file_count"] == 1
    assert payload["indexed_count"] == 0
    assert "manager_available" not in payload
    assert "refresh_job" not in payload
    refresh.assert_not_awaited()


def test_knowledge_bases_list_does_not_initialize_unused_configured_bases(tmp_path: Path) -> None:
    """Listing bases should not initialize every configured knowledge base."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _knowledge_config(docs, extra_base=True)
    _publish_committed_runtime_config(client.app, config)

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.get("/api/knowledge/bases")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert {base["name"] for base in payload["bases"]} == {"research", "unused"}
    assert all("manager_available" not in base for base in payload["bases"])
    assert all("refresh_job" not in base for base in payload["bases"])
    refresh.assert_not_awaited()


def test_status_and_list_use_persisted_indexed_count_without_refresh(tmp_path: Path) -> None:
    """Routine status endpoints keep metadata counts but do not report missing collections available."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, indexed_count=9)

    with (
        patch("mindroom.knowledge.manager._create_embedder", side_effect=AssertionError("embedder should not load")),
        patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh,
    ):
        status_response = client.get("/api/knowledge/bases/research/status")
        list_response = client.get("/api/knowledge/bases")
        files_response = client.get("/api/knowledge/bases/research/files")

    assert status_response.status_code == 200
    assert list_response.status_code == 200
    assert files_response.status_code == 200
    assert status_response.json()["indexed_count"] == 9
    assert list_response.json()["bases"][0]["indexed_count"] == 9
    assert "manager_available" not in status_response.json()
    assert "manager_available" not in list_response.json()["bases"][0]
    assert "manager_available" not in files_response.json()
    refresh.assert_not_awaited()


def test_status_reports_persisted_count_without_loading_collection(tmp_path: Path) -> None:
    """Routine status endpoints should not load Chroma collection handles."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, indexed_count=4)
    seen_embedders: list[str] = []

    class _BrokenVectorDb:
        def __init__(self, *, embedder: object, **_kwargs: object) -> None:
            seen_embedders.append(type(embedder).__name__)

        def exists(self) -> bool:
            msg = "corrupt collection"
            raise RuntimeError(msg)

    with (
        patch("mindroom.knowledge.manager.ChromaDb", _BrokenVectorDb),
        patch("mindroom.knowledge.manager._create_embedder", side_effect=AssertionError("embedder should not load")),
    ):
        response = client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    assert response.json()["indexed_count"] == 4
    assert "manager_available" not in response.json()
    assert seen_embedders == []


def test_status_does_not_probe_collection_availability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Status endpoints should report metadata without probing Chroma collection availability."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, indexed_count=4)

    def _offloaded_collection_probe(*_args: object) -> bool:
        msg = "status should not probe collection availability"
        raise AssertionError(msg)

    monkeypatch.setattr(knowledge_registry, "published_index_collection_exists_for_state", _offloaded_collection_probe)

    response = client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    assert response.json()["indexed_count"] == 4


@pytest.mark.parametrize(
    "refresh_state",
    [
        "stale",
        "refresh_failed",
    ],
)
def test_status_reports_queryable_last_good_index_when_refresh_state_is_not_ready(
    tmp_path: Path,
    refresh_state: str,
) -> None:
    """Refresh state should not hide a valid queryable index."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, indexed_count=7)
    key = resolve_published_index_key("research", config=config, runtime_paths=runtime_paths)
    if refresh_state == "stale":
        knowledge_registry.mark_published_index_stale(key, reason="source_changed")
    else:
        knowledge_registry.mark_published_index_refresh_failed_preserving_last_good(key, error="refresh failed")

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        status_response = client.get("/api/knowledge/bases/research/status")
        list_response = client.get("/api/knowledge/bases")

    assert status_response.status_code == 200
    assert list_response.status_code == 200
    status_payload = status_response.json()
    list_payload = list_response.json()["bases"][0]
    assert status_payload["indexed_count"] == 7
    assert list_payload["indexed_count"] == 7
    assert status_payload["refresh_state"] == refresh_state
    assert list_payload["refresh_state"] == refresh_state
    assert "manager_available" not in status_payload
    assert "manager_available" not in list_payload
    assert "refresh_job" not in status_payload
    assert "refresh_job" not in list_payload
    refresh.assert_not_awaited()


def test_status_rejects_query_incompatible_published_index(tmp_path: Path) -> None:
    """Dashboard indexed counts should still fail closed for indexes unsafe to query."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _knowledge_config(docs)
    _write_index_metadata(config, runtime_paths, indexed_count=9)
    key = resolve_published_index_key("research", config=config, runtime_paths=runtime_paths)
    metadata_path = published_index_metadata_path(key)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["settings"][3] = "different-embedder-provider"
    metadata_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    _publish_committed_runtime_config(client.app, config)

    with patch(
        "mindroom.knowledge.registry.published_index_collection_exists_for_state",
        side_effect=AssertionError("query-incompatible indexes should not probe collection availability"),
    ):
        response = client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    payload = response.json()
    assert "manager_available" not in payload
    assert payload["indexed_count"] == 0


def test_knowledge_files_use_managed_file_filters(tmp_path: Path) -> None:
    """File list and status counts should match the refresh/indexer file filters."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    (docs / "content" / "private").mkdir(parents=True)
    (docs / ".hidden").mkdir()
    (docs / "content" / "guide.md").write_text("managed", encoding="utf-8")
    (docs / "content" / "raw.txt").write_text("wrong extension", encoding="utf-8")
    (docs / "content" / "private" / "secret.md").write_text("excluded pattern", encoding="utf-8")
    (docs / ".hidden" / "note.md").write_text("hidden", encoding="utf-8")
    (docs / "outside.md").write_text("outside include pattern", encoding="utf-8")
    _init_git_checkout(
        docs,
        "content/guide.md",
        "content/raw.txt",
        "content/private/secret.md",
        ".hidden/note.md",
        "outside.md",
    )
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(
                path=str(docs),
                watch=False,
                include_extensions=[".md"],
                git=KnowledgeGitConfig(
                    repo_url="https://example.com/org/research.git",
                    include_patterns=["content/**"],
                    exclude_patterns=["content/private/**"],
                ),
            ),
        },
    )
    _publish_committed_runtime_config(client.app, config)

    files_response = client.get("/api/knowledge/bases/research/files")
    status_response = client.get("/api/knowledge/bases/research/status")

    assert files_response.status_code == 200
    assert status_response.status_code == 200
    assert files_response.json()["file_count"] == 1
    assert [entry["path"] for entry in files_response.json()["files"]] == ["content/guide.md"]
    assert status_response.json()["file_count"] == 1


def test_knowledge_files_under_symlinked_root_are_listed(tmp_path: Path) -> None:
    """API file listing should use the same resolved root as managed path discovery."""
    client = _test_client(tmp_path)
    actual_docs = tmp_path / "actual-docs"
    actual_docs.mkdir()
    (actual_docs / "guide.md").write_text("linked root", encoding="utf-8")
    docs_link = tmp_path / "docs-link"
    docs_link.symlink_to(actual_docs, target_is_directory=True)
    config = _knowledge_config(docs_link)
    _publish_committed_runtime_config(client.app, config)

    files_response = client.get("/api/knowledge/bases/research/files")
    status_response = client.get("/api/knowledge/bases/research/status")

    assert files_response.status_code == 200
    assert status_response.status_code == 200
    assert files_response.json()["file_count"] == 1
    assert [entry["path"] for entry in files_response.json()["files"]] == ["guide.md"]
    assert status_response.json()["file_count"] == 1


def test_git_backed_file_counts_use_tracked_semantic_files(tmp_path: Path) -> None:
    """Git-backed API file counts should match the tracked files the indexer can search."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "tracked.md").write_text("tracked", encoding="utf-8")
    (docs / "untracked.md").write_text("untracked", encoding="utf-8")
    _init_git_checkout(docs, "tracked.md")
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)

    files_response = client.get("/api/knowledge/bases/research/files")
    status_response = client.get("/api/knowledge/bases/research/status")
    list_response = client.get("/api/knowledge/bases")

    assert files_response.status_code == 200
    assert status_response.status_code == 200
    assert list_response.status_code == 200
    assert [entry["path"] for entry in files_response.json()["files"]] == ["tracked.md"]
    assert files_response.json()["file_count"] == 1
    assert status_response.json()["file_count"] == 1
    assert list_response.json()["bases"][0]["file_count"] == 1


def test_git_file_listing_timeout_degrades_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dashboard status should use a short Git listing timeout and degrade predictably."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)

    def _timed_out_git_listing(*_args: object, timeout_seconds: float, **_kwargs: object) -> list[Path]:
        assert timeout_seconds == 0.01
        msg = "Git command timed out after 0.01s: git ls-files -z"
        raise RuntimeError(msg)

    monkeypatch.setattr("mindroom.api.knowledge._DASHBOARD_GIT_FILE_LIST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.api.knowledge.list_git_tracked_managed_knowledge_files", _timed_out_git_listing)

    response = client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["file_count"] == 0
    assert payload["file_listing_degraded"] is True
    assert payload["file_listing_error"] == "Git command timed out after 0.01s: git ls-files -z"


def test_git_status_reads_disk_and_index_metadata(tmp_path: Path) -> None:
    """Git status should expose cheap disk/index facts without constructing a manager."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    _init_git_checkout(docs)
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(
        config,
        runtime_paths,
        revision="abc123",
        published_at="2026-04-24T12:34:56+00:00",
    )

    response = client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    git_status = response.json()["git"]
    assert git_status["syncing"] is False
    assert git_status["repo_present"] is True
    assert git_status["initial_sync_complete"] is True
    assert git_status["last_successful_commit"] == "abc123"
    assert git_status["last_successful_sync_at"] == "2026-04-24T12:34:56+00:00"


def test_git_status_redacts_last_refresh_error_from_metadata(tmp_path: Path) -> None:
    """Git refresh failures should be observable through status without leaking credentials."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(
        config,
        runtime_paths,
        last_error="Git command failed: https://token:secret@example.com/repo.git?token=query-secret#frag-secret",
    )

    response = client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["last_error"] == "Git command failed: https://***@example.com/repo.git"
    assert payload["git"]["last_error"] == "Git command failed: https://***@example.com/repo.git"
    assert "secret" not in json.dumps(payload)
    assert "query-secret" not in json.dumps(payload)
    assert "frag-secret" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_git_status_probe_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow Git checkout probes should run off the API event loop."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)
    loop = asyncio.get_running_loop()
    probe_started = asyncio.Event()

    def _slow_git_checkout_present(*_args: object, **_kwargs: object) -> bool:
        loop.call_soon_threadsafe(probe_started.set)
        time.sleep(0.2)
        return False

    async def _empty_file_info(*_args: object, **_kwargs: object) -> knowledge_api._FileListInfo:
        return knowledge_api._FileListInfo(files=[], total_size=0)

    monkeypatch.setattr(knowledge_api, "git_checkout_present", _slow_git_checkout_present)
    monkeypatch.setattr(knowledge_api, "_list_file_info", _empty_file_info)

    status_task = asyncio.create_task(
        knowledge_api.knowledge_status(
            "research",
            Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/knowledge/bases/research/status",
                    "headers": [],
                    "app": client.app,
                },
            ),
        ),
    )
    await asyncio.wait_for(probe_started.wait(), timeout=0.1)
    await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.05)

    assert status_task.done() is False
    payload = await status_task
    assert payload["git"]["repo_present"] is False


def test_api_lifespan_does_not_schedule_all_configured_knowledge_bases(tmp_path: Path) -> None:
    """API startup should load config but not warm every configured KB."""
    runtime_paths = _runtime_paths(tmp_path)
    config = _knowledge_config(tmp_path / "docs", extra_base=True)
    runtime_paths.config_path.write_text(json.dumps(config.authored_model_dump()), encoding="utf-8")
    main.initialize_api_app(main.app, runtime_paths)

    with (
        patch("mindroom.knowledge.refresh_scheduler.KnowledgeRefreshScheduler.schedule_refresh") as schedule,
        TestClient(main.app) as client,
    ):
        assert client.get("/api/health").status_code == 200

    schedule.assert_not_called()


def test_api_lifespan_prefers_orchestrator_refresh_scheduler(tmp_path: Path) -> None:
    """Bundled API should share the orchestrator scheduler instead of creating a second scheduler."""
    runtime_paths = _runtime_paths(tmp_path)
    scheduler = _RecordingRefreshScheduler()
    main.initialize_api_app(main.app, runtime_paths)
    main.app.state.orchestrator_knowledge_refresh_scheduler = scheduler

    try:
        with (
            patch("mindroom.api.main.KnowledgeRefreshScheduler") as api_owned_scheduler,
            TestClient(main.app) as client,
        ):
            assert client.get("/api/health").status_code == 200
            assert client.app.state.knowledge_refresh_scheduler is scheduler
        api_owned_scheduler.assert_not_called()
    finally:
        main.app.state.knowledge_refresh_scheduler = None
        with suppress(AttributeError):
            del main.app.state.orchestrator_knowledge_refresh_scheduler


def test_upload_schedules_refresh_without_inline_indexing(tmp_path: Path) -> None:
    """Uploads mutate files and schedule refresh instead of indexing inline."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", ("guide.md", b"hello", "text/markdown"))],
        )

    assert response.status_code == 200
    assert response.json()["uploaded"] == ["guide.md"]
    assert (docs / "guide.md").read_text(encoding="utf-8") == "hello"
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
    ]
    refresh.assert_not_awaited()


def test_upload_rejects_default_unsupported_extension_before_writing(tmp_path: Path) -> None:
    """Uploads must match the same semantic filters used by listing and indexing."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", ("diagram.png", b"\x89PNG\r\n\x1a\n", "image/png"))],
        )

    assert response.status_code == 415
    assert "not supported" in response.json()["detail"]
    assert not docs.exists()
    assert scheduler.scheduled == []
    refresh.assert_not_awaited()


@pytest.mark.parametrize(
    ("base_config", "filename"),
    [
        (KnowledgeBaseConfig(path="", watch=False, include_extensions=[".md"]), "notes.txt"),
        (KnowledgeBaseConfig(path="", watch=False, exclude_extensions=[".md"]), "guide.md"),
    ],
)
def test_upload_rejects_configured_extension_filter_exclusions(
    tmp_path: Path,
    base_config: KnowledgeBaseConfig,
    filename: str,
) -> None:
    """Configured include/exclude extension filters must reject uploads before bytes are staged."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    base_config.path = str(docs)
    config = Config(agents={}, models={}, knowledge_bases={"research": base_config})
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", (filename, b"hello", "text/plain"))],
        )

    assert response.status_code == 415
    assert "managed file filters" in response.json()["detail"]
    assert not docs.exists()
    assert scheduler.scheduled == []
    refresh.assert_not_awaited()


def test_upload_rejects_duplicate_normalized_multipart_filenames(tmp_path: Path) -> None:
    """A batch must not contain two parts that normalize to the same destination path."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.post(
            "/api/knowledge/bases/research/upload",
            files=[
                ("files", ("guide.md", b"first", "text/markdown")),
                ("files", ("nested/guide.md", b"second", "text/markdown")),
            ],
        )

    assert response.status_code == 409
    assert "duplicate destination 'guide.md'" in response.json()["detail"]
    assert not (docs / "guide.md").exists()
    assert scheduler.scheduled == []
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_upload_parts_are_noop_without_source_change_mark_or_refresh(tmp_path: Path) -> None:
    """Multipart parts without filenames should not mutate source availability."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with (
        patch("mindroom.api.knowledge.mark_knowledge_source_changed_async", side_effect=AssertionError("no mutation")),
        patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh,
    ):
        response = await knowledge_api.upload_knowledge_files(
            "research",
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/knowledge/bases/research/upload",
                    "headers": [],
                    "app": client.app,
                },
            ),
            [UploadFile(file=BytesIO(b"ignored"), filename="")],
        )

    assert response == {"base_id": "research", "uploaded": [], "count": 0}
    assert scheduler.scheduled == []
    refresh.assert_not_awaited()


def test_upload_schedules_refresh_for_duplicate_same_source_bases(tmp_path: Path) -> None:
    """Uploads to a shared source folder refresh every configured base reading that source."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _knowledge_config(docs, duplicate_source_base=True)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, base_id="research")
    _write_index_metadata(config, runtime_paths, base_id="summary", collection="summary_collection")
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", ("guide.md", b"hello", "text/markdown"))],
        )

    assert response.status_code == 200
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
        ("summary", config),
    ]
    refresh.assert_not_awaited()


def test_upload_source_change_mark_write_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload source-change-state I/O should be offloaded while the API mutation lock is held."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, base_id="research")
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler
    saw_running_loop: bool | None = None
    original_save = knowledge_registry.mark_published_index_stale

    def _offloaded_save(*args: object, **kwargs: object) -> object:
        nonlocal saw_running_loop
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            saw_running_loop = False
        else:
            saw_running_loop = True
        return original_save(*args, **kwargs)

    monkeypatch.setattr(knowledge_registry, "mark_published_index_stale", _offloaded_save)

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", ("guide.md", b"hello", "text/markdown"))],
        )

    assert response.status_code == 200
    assert saw_running_loop is False
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
    ]
    refresh.assert_not_awaited()


def test_upload_source_change_mark_failure_leaves_source_unchanged_and_schedules_refresh(tmp_path: Path) -> None:
    """Uploads schedule refresh when source-change state cannot be committed before replacement."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("old", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, base_id="research")
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    async def _fail_source_change_mark(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        msg = "source change mark failed"
        raise RuntimeError(msg)

    with (
        patch("mindroom.api.knowledge.mark_knowledge_source_changed_async", _fail_source_change_mark),
        patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh,
        pytest.raises(RuntimeError, match="source change mark failed"),
    ):
        client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", ("guide.md", b"new", "text/markdown"))],
        )

    assert (docs / "guide.md").read_text(encoding="utf-8") == "old"
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
    ]
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_cancellation_during_write_removes_temp_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload cancellation before replacement must not expose partial files."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    async def _cancel_stream(_upload: UploadFile, destination: Path, _filename: str) -> None:
        destination.write_text("partial", encoding="utf-8")
        raise asyncio.CancelledError

    monkeypatch.setattr(knowledge_api, "_stream_upload_to_destination", _cancel_stream)

    with pytest.raises(asyncio.CancelledError):
        await knowledge_api.upload_knowledge_files(
            "research",
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/knowledge/bases/research/upload",
                    "headers": [],
                    "app": client.app,
                },
            ),
            [UploadFile(file=BytesIO(b"new"), filename="guide.md")],
        )

    assert not (docs / "guide.md").exists()
    assert list(docs.glob(".*.upload.tmp")) == []
    assert scheduler.scheduled == []


@pytest.mark.asyncio
async def test_replacement_upload_cancellation_preserves_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled replacement uploads keep the previous complete file."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("old", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    async def _cancel_stream(_upload: UploadFile, destination: Path, _filename: str) -> None:
        destination.write_text("partial", encoding="utf-8")
        raise asyncio.CancelledError

    monkeypatch.setattr(knowledge_api, "_stream_upload_to_destination", _cancel_stream)

    with pytest.raises(asyncio.CancelledError):
        await knowledge_api.upload_knowledge_files(
            "research",
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/knowledge/bases/research/upload",
                    "headers": [],
                    "app": client.app,
                },
            ),
            [UploadFile(file=BytesIO(b"new"), filename="guide.md")],
        )

    assert (docs / "guide.md").read_text(encoding="utf-8") == "old"
    assert list(docs.glob(".*.upload.tmp")) == []
    assert scheduler.scheduled == []


@pytest.mark.asyncio
async def test_upload_cancellation_after_source_change_mark_finalizes_backup_and_schedules_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upload cancellation after source-change state commits should keep mutation cleanup and refresh scheduling."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("old", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler
    source_change_started = asyncio.Event()
    release_source_change = asyncio.Event()

    async def _slow_source_change_mark(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        source_change_started.set()
        await release_source_change.wait()
        return ("research",)

    monkeypatch.setattr(knowledge_api, "mark_knowledge_source_changed_async", _slow_source_change_mark)

    upload_task = asyncio.create_task(
        knowledge_api.upload_knowledge_files(
            "research",
            Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/knowledge/bases/research/upload",
                    "headers": [],
                    "app": client.app,
                },
            ),
            [UploadFile(file=BytesIO(b"new"), filename="guide.md")],
        ),
    )
    await source_change_started.wait()
    upload_task.cancel()
    release_source_change.set()

    with pytest.raises(asyncio.CancelledError):
        await upload_task

    assert (docs / "guide.md").read_text(encoding="utf-8") == "new"
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
    ]


def test_upload_write_failure_leaves_ready_index_unchanged_and_skips_refresh(
    tmp_path: Path,
) -> None:
    """Failed upload writes must not pre-mark unchanged indexes stale."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("old", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, base_id="research")
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    async def _fail_write(*_args: object, **_kwargs: object) -> None:
        msg = "write failed"
        raise RuntimeError(msg)

    with (
        patch("mindroom.api.knowledge._stream_upload_to_destination", _fail_write),
        patch(
            "mindroom.api.knowledge.mark_knowledge_source_changed_async",
            side_effect=AssertionError("no source change"),
        ),
        patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh,
        pytest.raises(RuntimeError, match="write failed"),
    ):
        client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", ("guide.md", b"new", "text/markdown"))],
        )

    metadata_path = published_index_metadata_path(
        resolve_published_index_key("research", config=config, runtime_paths=runtime_paths),
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "availability" not in metadata
    assert (docs / "guide.md").read_text(encoding="utf-8") == "old"
    assert scheduler.scheduled == []
    refresh.assert_not_awaited()


def test_upload_replace_failure_schedules_refresh_for_partial_commit(
    tmp_path: Path,
) -> None:
    """Failed upload replacement after a partial commit must still queue refresh."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("old guide", encoding="utf-8")
    (docs / "extra.md").write_text("old extra", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, base_id="research")
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler
    original_replace = type(docs).replace
    replace_count = 0

    def _fail_second_replace(self: object, target: object) -> object:
        nonlocal replace_count
        if isinstance(self, type(docs)) and self.name.endswith(".upload.tmp"):
            replace_count += 1
            if replace_count == 2:
                msg = "replace failed"
                raise RuntimeError(msg)
        return original_replace(self, target)

    with (
        patch("pathlib.Path.replace", _fail_second_replace),
        patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh,
        pytest.raises(RuntimeError, match="replace failed"),
    ):
        client.post(
            "/api/knowledge/bases/research/upload",
            files=[
                ("files", ("guide.md", b"new guide", "text/markdown")),
                ("files", ("extra.md", b"new extra", "text/markdown")),
            ],
        )

    assert (docs / "guide.md").read_text(encoding="utf-8") == "new guide"
    assert (docs / "extra.md").read_text(encoding="utf-8") == "old extra"
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
    ]
    refresh.assert_not_awaited()


def test_git_backed_upload_is_rejected_before_creating_cold_checkout(tmp_path: Path) -> None:
    """Uploads must not create a non-Git directory where a later clone will fail."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    response = client.post(
        "/api/knowledge/bases/research/upload",
        files=[("files", ("guide.md", b"hello", "text/markdown"))],
    )

    assert response.status_code == 409
    assert "Git-backed" in response.json()["detail"]
    assert not docs.exists()
    assert scheduler.scheduled == []


def test_upload_to_local_base_sharing_git_source_is_rejected(tmp_path: Path) -> None:
    """A local alias of a Git-backed source must not bypass Git mutation restrictions."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(docs), watch=False),
            "summary": KnowledgeBaseConfig(
                path=str(docs),
                watch=False,
                git=KnowledgeGitConfig(repo_url="https://example.com/org/research.git"),
            ),
        },
    )
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    response = client.post(
        "/api/knowledge/bases/research/upload",
        files=[("files", ("guide.md", b"hello", "text/markdown"))],
    )

    assert response.status_code == 409
    assert "Git-backed" in response.json()["detail"]
    assert not docs.exists()
    assert scheduler.scheduled == []


def test_upload_to_child_of_git_source_is_rejected(tmp_path: Path) -> None:
    """A local child alias inside a Git-backed source must not accept dashboard uploads."""
    client = _test_client(tmp_path)
    repo = tmp_path / "repo"
    child = repo / "docs"
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(child), watch=False),
            "repo": KnowledgeBaseConfig(
                path=str(repo),
                watch=False,
                git=KnowledgeGitConfig(repo_url="https://example.com/org/research.git"),
            ),
        },
    )
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    response = client.post(
        "/api/knowledge/bases/research/upload",
        files=[("files", ("guide.md", b"hello", "text/markdown"))],
    )

    assert response.status_code == 409
    assert "Git-backed" in response.json()["detail"]
    assert not (child / "guide.md").exists()
    assert scheduler.scheduled == []


def test_upload_to_parent_alias_over_git_source_path_is_rejected(tmp_path: Path) -> None:
    """A parent local alias must not replace the path reserved for a Git-backed source."""
    client = _test_client(tmp_path)
    root = tmp_path / "knowledge"
    repo = root / "repo"
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(root), watch=False),
            "repo": KnowledgeBaseConfig(
                path=str(repo),
                watch=False,
                git=KnowledgeGitConfig(repo_url="https://example.com/org/research.git"),
            ),
        },
    )
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    response = client.post(
        "/api/knowledge/bases/research/upload",
        files=[("files", ("repo", b"hello", "text/markdown"))],
    )

    assert response.status_code == 409
    assert "Git-backed" in response.json()["detail"]
    assert not repo.exists()
    assert scheduler.scheduled == []


def test_upload_over_existing_directory_is_rejected_before_mutation(tmp_path: Path) -> None:
    """Uploads must not replace an existing directory with a file."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    target_dir = docs / "guide.md"
    target_dir.mkdir(parents=True)
    (target_dir / "nested.txt").write_text("keep me", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", ("guide.md", b"hello", "text/markdown"))],
        )

    assert response.status_code == 409
    assert "not a regular file" in response.json()["detail"]
    assert target_dir.is_dir()
    assert (target_dir / "nested.txt").read_text(encoding="utf-8") == "keep me"
    assert list(docs.glob("*.upload.*")) == []
    assert scheduler.scheduled == []
    refresh.assert_not_awaited()


def test_delete_schedules_refresh_without_inline_indexing(tmp_path: Path) -> None:
    """Deletes mutate files and schedule refresh instead of editing vectors inline."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.delete("/api/knowledge/bases/research/files/guide.md")

    assert response.status_code == 200
    assert not (docs / "guide.md").exists()
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
    ]
    refresh.assert_not_awaited()


@pytest.mark.parametrize(
    ("literal_path", "decoded_path"),
    [
        ("a%2Fb.md", "a/b.md"),
        ("%2e%2e.md", "...md"),
    ],
)
@pytest.mark.asyncio
async def test_delete_uses_once_decoded_route_path_for_percent_bearing_filenames(
    tmp_path: Path,
    literal_path: str,
    decoded_path: str,
) -> None:
    """A dashboard-encoded literal percent filename must not be decoded into another path."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    literal_file = docs / literal_path
    decoded_file = docs / decoded_path
    literal_file.parent.mkdir(parents=True, exist_ok=True)
    decoded_file.parent.mkdir(parents=True, exist_ok=True)
    literal_file.write_text("literal", encoding="utf-8")
    decoded_file.write_text("decoded", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, base_id="research")
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = await knowledge_api.delete_knowledge_file(
            "research",
            literal_path,
            Request(
                {
                    "type": "http",
                    "method": "DELETE",
                    "path": f"/api/knowledge/bases/research/files/{literal_path}",
                    "headers": [],
                    "app": client.app,
                },
            ),
        )

    assert response["path"] == literal_path
    assert not literal_file.exists()
    assert decoded_file.read_text(encoding="utf-8") == "decoded"
    key = resolve_published_index_key("research", config=config, runtime_paths=runtime_paths)
    state = load_published_index_state(published_index_metadata_path(key))
    assert knowledge_registry.published_index_refresh_state(state) == "stale"
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
    ]
    refresh.assert_not_awaited()


def test_delete_rejects_default_unsupported_extension_without_mutation(tmp_path: Path) -> None:
    """Deletes must be limited to the same managed semantic files exposed by listing."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    image = docs / "diagram.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.delete("/api/knowledge/bases/research/files/diagram.png")

    assert response.status_code == 415
    assert "managed file filters" in response.json()["detail"]
    assert image.read_bytes() == b"\x89PNG\r\n\x1a\n"
    assert scheduler.scheduled == []
    refresh.assert_not_awaited()


@pytest.mark.parametrize(
    ("base_config", "filename"),
    [
        (KnowledgeBaseConfig(path="", watch=False, include_extensions=[".md"]), "notes.txt"),
        (KnowledgeBaseConfig(path="", watch=False, exclude_extensions=[".md"]), "guide.md"),
    ],
)
def test_delete_rejects_configured_extension_filter_exclusions(
    tmp_path: Path,
    base_config: KnowledgeBaseConfig,
    filename: str,
) -> None:
    """Configured include/exclude filters must also bound dashboard deletes."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    target = docs / filename
    target.write_text("hello", encoding="utf-8")
    base_config.path = str(docs)
    config = Config(agents={}, models={}, knowledge_bases={"research": base_config})
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.delete(f"/api/knowledge/bases/research/files/{filename}")

    assert response.status_code == 415
    assert "managed file filters" in response.json()["detail"]
    assert target.read_text(encoding="utf-8") == "hello"
    assert scheduler.scheduled == []
    refresh.assert_not_awaited()


def test_delete_schedules_refresh_for_duplicate_same_source_bases(tmp_path: Path) -> None:
    """Deletes from a shared source folder refresh every configured base reading that source."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs, duplicate_source_base=True)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, base_id="research")
    _write_index_metadata(config, runtime_paths, base_id="summary", collection="summary_collection")
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.delete("/api/knowledge/bases/research/files/guide.md")

    assert response.status_code == 200
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
        ("summary", config),
    ]
    refresh.assert_not_awaited()


def test_delete_source_change_mark_failure_keeps_source_change_and_schedules_refresh(tmp_path: Path) -> None:
    """Deletes schedule refresh when source-change state cannot be committed after the source write."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, base_id="research")
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    async def _fail_source_change_mark(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        msg = "source change mark failed"
        raise RuntimeError(msg)

    with (
        patch("mindroom.api.knowledge.mark_knowledge_source_changed_async", _fail_source_change_mark),
        patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh,
        pytest.raises(RuntimeError, match="source change mark failed"),
    ):
        client.delete("/api/knowledge/bases/research/files/guide.md")

    assert not (docs / "guide.md").exists()
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
    ]
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_cancellation_after_source_change_mark_removes_backup_and_schedules_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete cancellation after source-change state commits should finalize backup cleanup and schedule refresh."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler
    source_change_started = asyncio.Event()
    release_source_change = asyncio.Event()

    async def _slow_source_change_mark(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        source_change_started.set()
        await release_source_change.wait()
        return ("research",)

    monkeypatch.setattr(knowledge_api, "mark_knowledge_source_changed_async", _slow_source_change_mark)

    delete_task = asyncio.create_task(
        knowledge_api.delete_knowledge_file(
            "research",
            "guide.md",
            Request(
                {
                    "type": "http",
                    "method": "DELETE",
                    "path": "/api/knowledge/bases/research/files/guide.md",
                    "headers": [],
                    "app": client.app,
                },
            ),
        ),
    )
    await source_change_started.wait()
    delete_task.cancel()
    release_source_change.set()

    with pytest.raises(asyncio.CancelledError):
        await delete_task

    assert not (docs / "guide.md").exists()
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in scheduler.scheduled] == [
        ("research", config),
    ]


def test_delete_filesystem_failure_leaves_ready_index_unchanged_and_skips_refresh(
    tmp_path: Path,
) -> None:
    """Failed delete mutations must not pre-mark unchanged indexes stale."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(config, runtime_paths, base_id="research")
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    def _fail_delete_stage(*_args: object, **_kwargs: object) -> object:
        msg = "unlink failed"
        raise RuntimeError(msg)

    with (
        patch("pathlib.Path.unlink", _fail_delete_stage),
        patch(
            "mindroom.api.knowledge.mark_knowledge_source_changed_async",
            side_effect=AssertionError("no source change"),
        ),
        patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh,
        pytest.raises(RuntimeError, match="unlink failed"),
    ):
        client.delete("/api/knowledge/bases/research/files/guide.md")

    metadata_path = published_index_metadata_path(
        resolve_published_index_key("research", config=config, runtime_paths=runtime_paths),
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "availability" not in metadata
    assert (docs / "guide.md").read_text(encoding="utf-8") == "hello"
    assert scheduler.scheduled == []
    refresh.assert_not_awaited()


def test_git_backed_delete_is_rejected_without_mutating_checkout(tmp_path: Path) -> None:
    """Deletes from Git-backed checkouts are rejected because refresh hard-resets from remote."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    response = client.delete("/api/knowledge/bases/research/files/guide.md")

    assert response.status_code == 409
    assert "Git-backed" in response.json()["detail"]
    assert (docs / "guide.md").read_text(encoding="utf-8") == "hello"
    assert scheduler.scheduled == []


def test_delete_from_local_base_sharing_git_source_is_rejected(tmp_path: Path) -> None:
    """Deletes through a local alias must not mutate a Git-backed source directory."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(docs), watch=False),
            "summary": KnowledgeBaseConfig(
                path=str(docs),
                watch=False,
                git=KnowledgeGitConfig(repo_url="https://example.com/org/research.git"),
            ),
        },
    )
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    response = client.delete("/api/knowledge/bases/research/files/guide.md")

    assert response.status_code == 409
    assert "Git-backed" in response.json()["detail"]
    assert (docs / "guide.md").read_text(encoding="utf-8") == "hello"
    assert scheduler.scheduled == []


def test_delete_from_child_of_git_source_is_rejected(tmp_path: Path) -> None:
    """Deletes through a child local alias must not mutate a parent Git-backed source."""
    client = _test_client(tmp_path)
    repo = tmp_path / "repo"
    child = repo / "docs"
    child.mkdir(parents=True)
    (child / "guide.md").write_text("hello", encoding="utf-8")
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(child), watch=False),
            "repo": KnowledgeBaseConfig(
                path=str(repo),
                watch=False,
                git=KnowledgeGitConfig(repo_url="https://example.com/org/research.git"),
            ),
        },
    )
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    response = client.delete("/api/knowledge/bases/research/files/guide.md")

    assert response.status_code == 409
    assert "Git-backed" in response.json()["detail"]
    assert (child / "guide.md").read_text(encoding="utf-8") == "hello"
    assert scheduler.scheduled == []


def test_delete_from_parent_alias_inside_git_source_is_rejected(tmp_path: Path) -> None:
    """Deletes through a parent local alias must not remove files inside a Git-backed child source."""
    client = _test_client(tmp_path)
    root = tmp_path / "knowledge"
    repo = root / "repo"
    repo.mkdir(parents=True)
    (repo / "guide.md").write_text("hello", encoding="utf-8")
    config = Config(
        agents={},
        models={},
        knowledge_bases={
            "research": KnowledgeBaseConfig(path=str(root), watch=False),
            "repo": KnowledgeBaseConfig(
                path=str(repo),
                watch=False,
                git=KnowledgeGitConfig(repo_url="https://example.com/org/research.git"),
            ),
        },
    )
    _publish_committed_runtime_config(client.app, config)
    scheduler = _RecordingRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    response = client.delete("/api/knowledge/bases/research/files/repo/guide.md")

    assert response.status_code == 409
    assert "Git-backed" in response.json()["detail"]
    assert (repo / "guide.md").read_text(encoding="utf-8") == "hello"
    assert scheduler.scheduled == []


def test_explicit_reindex_uses_refresh_runner(tmp_path: Path) -> None:
    """Admin reindex remains blocking but uses the same refresh runner."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)

    with patch(
        "mindroom.api.knowledge.refresh_knowledge_binding",
        new=AsyncMock(
            return_value=SimpleNamespace(
                indexed_count=7,
                index_published=True,
                availability=KnowledgeAvailability.READY,
                last_error=None,
            ),
        ),
    ) as refresh:
        response = client.post("/api/knowledge/bases/research/reindex")

    assert response.status_code == 200
    assert response.json()["indexed_count"] == 7
    refresh.assert_awaited_once_with(
        "research",
        config=config,
        runtime_paths=main._app_context(client.app).runtime_paths,
        force_reindex=True,
    )


def test_explicit_reindex_uses_refresh_scheduler_when_available(tmp_path: Path) -> None:
    """Admin reindex should replace stale queued scheduler work instead of bypassing the scheduler."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    runtime_paths = main._app_context(client.app).runtime_paths

    class _ManualRefreshScheduler(_RecordingRefreshScheduler):
        def __init__(self) -> None:
            super().__init__()
            self.manual_calls: list[tuple[str, Config, RuntimePaths, bool]] = []

        async def refresh_now(
            self,
            base_id: str,
            *,
            config: Config,
            runtime_paths: RuntimePaths,
            execution_identity: object | None = None,
            force_reindex: bool = False,
        ) -> object:
            _ = execution_identity
            self.manual_calls.append((base_id, config, runtime_paths, force_reindex))
            return SimpleNamespace(
                indexed_count=11,
                index_published=True,
                availability=KnowledgeAvailability.READY,
                last_error=None,
            )

    scheduler = _ManualRefreshScheduler()
    client.app.state.knowledge_refresh_scheduler = scheduler

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.post("/api/knowledge/bases/research/reindex")

    assert response.status_code == 200
    assert response.json()["indexed_count"] == 11
    assert scheduler.manual_calls == [("research", config, runtime_paths, True)]
    refresh.assert_not_awaited()


def test_explicit_reindex_returns_conflict_when_no_index_is_published(tmp_path: Path) -> None:
    """Admin reindex must not report success when refresh leaves no usable index."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)

    with patch(
        "mindroom.api.knowledge.refresh_knowledge_binding",
        new=AsyncMock(
            return_value=SimpleNamespace(
                indexed_count=0,
                index_published=False,
                availability=KnowledgeAvailability.REFRESH_FAILED,
                last_error="Indexed 0 of 1 managed knowledge files",
            ),
        ),
    ):
        response = client.post("/api/knowledge/bases/research/reindex")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["success"] is False
    assert detail["availability"] == "refresh_failed"
    assert detail["last_error"] == "Indexed 0 of 1 managed knowledge files"


def test_explicit_reindex_returns_conflict_when_last_good_is_not_ready(tmp_path: Path) -> None:
    """Admin reindex success requires a newly READY index, not only preserved last-good vectors."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)

    with patch(
        "mindroom.api.knowledge.refresh_knowledge_binding",
        new=AsyncMock(
            return_value=SimpleNamespace(
                indexed_count=3,
                index_published=True,
                availability=KnowledgeAvailability.CONFIG_MISMATCH,
                last_error="chunking config changed",
            ),
        ),
    ):
        response = client.post("/api/knowledge/bases/research/reindex")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["success"] is False
    assert detail["base_id"] == "research"
    assert detail["indexed_count"] == 3
    assert detail["availability"] == "config_mismatch"
    assert detail["last_error"] == "chunking config changed"


def test_explicit_reindex_returns_structured_failure_when_refresh_raises(tmp_path: Path) -> None:
    """Operational refresh exceptions should not become unstructured 500 responses."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)

    with patch(
        "mindroom.api.knowledge.refresh_knowledge_binding",
        new=AsyncMock(side_effect=RuntimeError("Git failed https://token:secret@example.com/repo.git")),
    ):
        response = client.post("/api/knowledge/bases/research/reindex")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["success"] is False
    assert detail["base_id"] == "research"
    assert detail["availability"] == "refresh_failed"
    assert detail["indexed_count"] == 0
    assert detail["last_error"] == "Git failed https://***@example.com/repo.git"


def test_explicit_reindex_redacts_metadata_last_error_on_failure(tmp_path: Path) -> None:
    """Reindex failure responses must redact persisted metadata errors."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)
    _write_index_metadata(
        config,
        runtime_paths,
        last_error="Git failed https://token:secret@example.com/repo.git?token=query-secret#frag-secret",
        indexed_count=2,
    )

    with patch(
        "mindroom.api.knowledge.refresh_knowledge_binding",
        new=AsyncMock(side_effect=RuntimeError("ignored raw failure")),
    ):
        response = client.post("/api/knowledge/bases/research/reindex")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["indexed_count"] == 2
    assert detail["last_error"] == "Git failed https://***@example.com/repo.git"
    assert "secret" not in json.dumps(detail)
    assert "query-secret" not in json.dumps(detail)
    assert "frag-secret" not in json.dumps(detail)


def test_status_degrades_gracefully_when_index_key_resolution_fails(tmp_path: Path) -> None:
    """Status should still return file facts when index metadata cannot be resolved."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)

    with patch("mindroom.api.knowledge.get_knowledge_index_status", side_effect=ValueError("bad binding")):
        response = client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["file_count"] == 1
    assert payload["indexed_count"] == 0
    assert "manager_available" not in payload
