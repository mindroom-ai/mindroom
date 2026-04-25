"""Tests for non-initializing knowledge management API routes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import main
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.knowledge.registry import resolve_snapshot_key, snapshot_metadata_path

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _knowledge_config(path: Path, *, extra_base: bool = False, git: bool = False) -> Config:
    knowledge_bases = {
        "research": KnowledgeBaseConfig(
            path=str(path),
            watch=False,
            git=KnowledgeGitConfig(repo_url="https://example.com/org/research.git") if git else None,
        ),
    }
    if extra_base:
        knowledge_bases["unused"] = KnowledgeBaseConfig(path=str(path.parent / "unused"), watch=False)
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


def _write_snapshot_metadata(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    base_id: str = "research",
    collection: str = "published_collection",
    revision: str | None = None,
    published_at: str | None = None,
) -> None:
    key = resolve_snapshot_key(base_id, config=config, runtime_paths=runtime_paths)
    metadata_path = snapshot_metadata_path(key)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "settings": list(key.indexing_settings),
        "status": "complete",
        "collection": collection,
        "availability": "ready",
    }
    if revision is not None:
        payload["published_revision"] = revision
    if published_at is not None:
        payload["last_published_at"] = published_at
    metadata_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _test_client(tmp_path: Path) -> TestClient:
    runtime_paths = _runtime_paths(tmp_path)
    main.initialize_api_app(main.app, runtime_paths)
    return TestClient(main.app)


class _RecordingRefreshOwner:
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


def test_knowledge_status_reads_snapshot_metadata_without_initializing(tmp_path: Path) -> None:
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
    assert payload["manager_available"] is False
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
    assert all(base["manager_available"] is False for base in payload["bases"])
    refresh.assert_not_awaited()


def test_knowledge_files_use_managed_file_filters(tmp_path: Path) -> None:
    """File list and status counts should match the refresh/indexer file filters."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    (docs / "content" / "private").mkdir(parents=True)
    (docs / ".git").mkdir()
    (docs / ".hidden").mkdir()
    (docs / "content" / "guide.md").write_text("managed", encoding="utf-8")
    (docs / "content" / "raw.txt").write_text("wrong extension", encoding="utf-8")
    (docs / "content" / "private" / "secret.md").write_text("excluded pattern", encoding="utf-8")
    (docs / ".git" / "config.md").write_text("git internals", encoding="utf-8")
    (docs / ".hidden" / "note.md").write_text("hidden", encoding="utf-8")
    (docs / "outside.md").write_text("outside include pattern", encoding="utf-8")
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


def test_git_status_reads_disk_and_snapshot_metadata(tmp_path: Path) -> None:
    """Git status should expose cheap disk/snapshot facts without constructing a manager."""
    client = _test_client(tmp_path)
    runtime_paths = main._app_context(client.app).runtime_paths
    docs = tmp_path / "docs"
    (docs / ".git").mkdir(parents=True)
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)
    _write_snapshot_metadata(
        config,
        runtime_paths,
        revision="abc123",
        published_at="2026-04-24T12:34:56+00:00",
    )

    response = client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    git_status = response.json()["git"]
    assert git_status["repo_present"] is True
    assert git_status["initial_sync_complete"] is True
    assert git_status["last_successful_commit"] == "abc123"
    assert git_status["last_successful_sync_at"] == "2026-04-24T12:34:56+00:00"


def test_api_lifespan_does_not_schedule_all_configured_knowledge_bases(tmp_path: Path) -> None:
    """API startup should load config but not warm every configured KB."""
    runtime_paths = _runtime_paths(tmp_path)
    config = _knowledge_config(tmp_path / "docs", extra_base=True)
    runtime_paths.config_path.write_text(json.dumps(config.authored_model_dump()), encoding="utf-8")
    main.initialize_api_app(main.app, runtime_paths)

    with (
        patch("mindroom.knowledge.refresh_owner.StandaloneKnowledgeRefreshOwner.schedule_initial_load") as schedule,
        TestClient(main.app) as client,
    ):
        assert client.get("/api/health").status_code == 200

    schedule.assert_not_called()


def test_upload_schedules_refresh_without_inline_indexing(tmp_path: Path) -> None:
    """Uploads mutate files and schedule refresh instead of indexing inline."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    owner = _RecordingRefreshOwner()
    client.app.state.knowledge_refresh_owner = owner

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", ("guide.md", b"hello", "text/markdown"))],
        )

    assert response.status_code == 200
    assert response.json()["uploaded"] == ["guide.md"]
    assert (docs / "guide.md").read_text(encoding="utf-8") == "hello"
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in owner.scheduled] == [("research", config)]
    refresh.assert_not_awaited()


def test_git_backed_upload_is_rejected_before_creating_cold_checkout(tmp_path: Path) -> None:
    """Uploads must not create a non-Git directory where a later clone will fail."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)
    owner = _RecordingRefreshOwner()
    client.app.state.knowledge_refresh_owner = owner

    response = client.post(
        "/api/knowledge/bases/research/upload",
        files=[("files", ("guide.md", b"hello", "text/markdown"))],
    )

    assert response.status_code == 409
    assert "Git-backed" in response.json()["detail"]
    assert not docs.exists()
    assert owner.scheduled == []


def test_delete_schedules_refresh_without_inline_indexing(tmp_path: Path) -> None:
    """Deletes mutate files and schedule refresh instead of editing vectors inline."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)
    owner = _RecordingRefreshOwner()
    client.app.state.knowledge_refresh_owner = owner

    with patch("mindroom.api.knowledge.refresh_knowledge_binding", new=AsyncMock()) as refresh:
        response = client.delete("/api/knowledge/bases/research/files/guide.md")

    assert response.status_code == 200
    assert not (docs / "guide.md").exists()
    assert [(base_id, scheduled_config) for base_id, scheduled_config, _ in owner.scheduled] == [("research", config)]
    refresh.assert_not_awaited()


def test_git_backed_delete_is_rejected_without_mutating_checkout(tmp_path: Path) -> None:
    """Deletes from Git-backed checkouts are rejected because refresh hard-resets from remote."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("hello", encoding="utf-8")
    config = _knowledge_config(docs, git=True)
    _publish_committed_runtime_config(client.app, config)
    owner = _RecordingRefreshOwner()
    client.app.state.knowledge_refresh_owner = owner

    response = client.delete("/api/knowledge/bases/research/files/guide.md")

    assert response.status_code == 409
    assert "Git-backed" in response.json()["detail"]
    assert (docs / "guide.md").read_text(encoding="utf-8") == "hello"
    assert owner.scheduled == []


def test_explicit_reindex_uses_refresh_runner(tmp_path: Path) -> None:
    """Admin reindex remains blocking but uses the same refresh runner."""
    client = _test_client(tmp_path)
    docs = tmp_path / "docs"
    config = _knowledge_config(docs)
    _publish_committed_runtime_config(client.app, config)

    with patch(
        "mindroom.api.knowledge.refresh_knowledge_binding",
        new=AsyncMock(return_value=SimpleNamespace(indexed_count=7)),
    ) as refresh:
        response = client.post("/api/knowledge/bases/research/reindex")

    assert response.status_code == 200
    assert response.json()["indexed_count"] == 7
    refresh.assert_awaited_once_with(
        "research",
        config=config,
        runtime_paths=main._app_context(client.app).runtime_paths,
    )
