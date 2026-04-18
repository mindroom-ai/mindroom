"""Tests for knowledge management API routes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import mindroom.api.knowledge as knowledge_api
from mindroom import constants
from mindroom.api import main
from mindroom.config.knowledge import KnowledgeBaseConfig, KnowledgeGitConfig
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.knowledge.manager import initialize_shared_knowledge_managers, shutdown_shared_knowledge_managers


def _knowledge_config(
    path: Path,
    *,
    base_id: str = "research",
    with_git: bool = False,
) -> Config:
    git_config = (
        KnowledgeGitConfig(
            repo_url="https://github.com/example/private-repo.git",
            branch="main",
            poll_interval_seconds=300,
        )
        if with_git
        else None
    )
    return Config(
        agents={},
        models={},
        knowledge_bases={
            base_id: KnowledgeBaseConfig(
                path=str(path),
                watch=False,
                git=git_config,
            ),
        },
    )


def _publish_committed_runtime_config(api_app: object, config: Config) -> None:
    """Publish one committed config snapshot for request-path tests."""
    context = main._app_context(api_app)
    context.config_data = config.authored_model_dump()
    context.runtime_config = config
    context.config_load_result = main.ConfigLoadResult(success=True)


class _DummyCollection:
    def get(self, *, limit: int | None = None, include: list[str] | None = None) -> dict[str, object]:
        _ = limit, include
        return {"ids": []}


class _DummyClient:
    def get_collection(self, name: str) -> _DummyCollection:
        _ = name
        return _DummyCollection()


class _DummyChromaDb:
    def __init__(self, **_: object) -> None:
        self.collection_name = "mindroom_knowledge"
        self.client = _DummyClient()

    def delete(self) -> bool:
        return True

    def create(self) -> None:
        return None

    def exists(self) -> bool:
        return True


class _DummyKnowledge:
    def __init__(self, vector_db: _DummyChromaDb) -> None:
        self.vector_db = vector_db


@pytest.fixture
def test_client(tmp_path: Path) -> TestClient:
    """Create an API client bound to explicit runtime paths for this test file."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    main.initialize_api_app(main.app, runtime_paths)
    return TestClient(main.app)


def test_knowledge_bases_list_uses_existing_manager_without_initializing(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Base listing should use an existing manager without triggering initialization."""
    config = _knowledge_config(tmp_path, with_git=True)
    manager = MagicMock()
    manager.get_status.return_value = {
        "indexed_count": 3,
        "file_count": 4,
        "git": {
            "repo_url": "https://github.com/example/private-repo.git",
            "branch": "main",
            "lfs": True,
            "startup_behavior": "background",
            "syncing": False,
            "repo_present": True,
            "initial_sync_complete": True,
            "last_successful_sync_at": "2026-04-17T12:00:00+00:00",
            "last_successful_commit": "abc123",
            "last_error": None,
            "pending_startup_mode": None,
        },
    }
    _publish_committed_runtime_config(test_client.app, config)

    with (
        patch(
            "mindroom.api.knowledge.get_shared_knowledge_manager_for_config",
            return_value=manager,
        ) as get_manager,
        patch(
            "mindroom.api.knowledge.initialize_shared_knowledge_managers",
            new=AsyncMock(),
        ) as init_managers,
    ):
        response = test_client.get("/api/knowledge/bases")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["bases"][0]["name"] == "research"
    assert payload["bases"][0]["indexed_count"] == 3
    assert payload["bases"][0]["file_count"] == 4
    assert payload["bases"][0]["manager_available"] is True
    assert payload["bases"][0]["git"] == {
        "repo_url": "https://github.com/example/private-repo.git",
        "branch": "main",
        "lfs": True,
        "startup_behavior": "background",
        "syncing": False,
        "repo_present": True,
        "initial_sync_complete": True,
        "last_successful_sync_at": "2026-04-17T12:00:00+00:00",
        "last_successful_commit": "abc123",
        "last_error": None,
        "pending_startup_mode": None,
    }
    get_manager.assert_called_once()
    init_managers.assert_not_awaited()


def test_knowledge_root_resolves_relative_path_from_config_dir(
    tmp_path: Path,
) -> None:
    """Knowledge API should resolve relative base paths from the config directory."""
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    config = _knowledge_config(path=Path("knowledge"))
    runtime_paths = resolve_runtime_paths(
        config_path=config_dir / "config.yaml",
        storage_path=tmp_path / "storage",
    )

    root = knowledge_api._knowledge_root(config, "research", runtime_paths)

    assert root == (config_dir / "knowledge").resolve()


def test_knowledge_files_list_uses_manager_filters_when_available(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """File listing should reflect the manager-managed subset of files."""
    config = _knowledge_config(tmp_path)
    included_file = tmp_path / "docs" / "guide.md"
    included_file.parent.mkdir(parents=True, exist_ok=True)
    included_file.write_text("guide", encoding="utf-8")
    excluded_file = tmp_path / "src" / "code.py"
    excluded_file.parent.mkdir(parents=True, exist_ok=True)
    excluded_file.write_text("print('x')", encoding="utf-8")

    manager = MagicMock()
    manager.list_files.return_value = [included_file]
    _publish_committed_runtime_config(test_client.app, config)

    with (
        patch(
            "mindroom.api.knowledge.get_shared_knowledge_manager_for_config",
            return_value=manager,
        ) as get_manager,
        patch(
            "mindroom.api.knowledge.initialize_shared_knowledge_managers",
            new=AsyncMock(),
        ) as init_managers,
    ):
        response = test_client.get("/api/knowledge/bases/research/files")

    assert response.status_code == 200
    payload = response.json()
    assert payload["file_count"] == 1
    assert payload["files"] == [
        {
            "name": "guide.md",
            "path": "docs/guide.md",
            "size": included_file.stat().st_size,
            "modified": payload["files"][0]["modified"],
            "type": "md",
        },
    ]
    assert payload["manager_available"] is True
    get_manager.assert_called_once()
    init_managers.assert_not_awaited()


def test_knowledge_status_uses_existing_manager_without_initializing(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Status should use an existing manager without triggering initialization."""
    config = _knowledge_config(tmp_path, with_git=True)
    manager = MagicMock()
    manager.get_status.return_value = {
        "indexed_count": 3,
        "file_count": 4,
        "git": {
            "repo_url": "https://github.com/example/private-repo.git",
            "branch": "main",
            "lfs": False,
            "startup_behavior": "blocking",
            "syncing": True,
            "repo_present": False,
            "initial_sync_complete": False,
            "last_successful_sync_at": None,
            "last_successful_commit": None,
            "last_error": "fetch failed",
            "pending_startup_mode": "resume",
        },
    }
    _publish_committed_runtime_config(test_client.app, config)

    with (
        patch(
            "mindroom.api.knowledge.get_shared_knowledge_manager_for_config",
            return_value=manager,
        ) as get_manager,
        patch(
            "mindroom.api.knowledge.initialize_shared_knowledge_managers",
            new=AsyncMock(),
        ) as init_managers,
    ):
        response = test_client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    assert response.json() == {
        "base_id": "research",
        "folder_path": str(tmp_path.resolve()),
        "watch": False,
        "file_count": 4,
        "indexed_count": 3,
        "manager_available": True,
        "git": {
            "repo_url": "https://github.com/example/private-repo.git",
            "branch": "main",
            "lfs": False,
            "startup_behavior": "blocking",
            "syncing": True,
            "repo_present": False,
            "initial_sync_complete": False,
            "last_successful_sync_at": None,
            "last_successful_commit": None,
            "last_error": "fetch failed",
            "pending_startup_mode": "resume",
        },
    }
    get_manager.assert_called_once()
    init_managers.assert_not_awaited()


def test_knowledge_status_falls_back_without_initializing_when_manager_missing(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Status should remain fast and best-effort when no manager exists yet."""
    config = _knowledge_config(tmp_path)
    (tmp_path / "note.md").write_text("hello", encoding="utf-8")
    _publish_committed_runtime_config(test_client.app, config)

    with (
        patch(
            "mindroom.api.knowledge.get_shared_knowledge_manager_for_config",
            return_value=None,
        ) as get_manager,
        patch(
            "mindroom.api.knowledge.initialize_shared_knowledge_managers",
            new=AsyncMock(),
        ) as init_managers,
    ):
        response = test_client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 200
    assert response.json() == {
        "base_id": "research",
        "folder_path": str(tmp_path.resolve()),
        "watch": False,
        "file_count": 1,
        "indexed_count": 0,
        "manager_available": False,
    }
    get_manager.assert_called_once()
    init_managers.assert_not_awaited()


def test_knowledge_upload_rolls_back_on_oversized_file(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When one file is too large, previous files in the same request are removed."""
    config = _knowledge_config(tmp_path)
    monkeypatch.setattr("mindroom.api.knowledge._MAX_UPLOAD_BYTES", 5)
    _publish_committed_runtime_config(test_client.app, config)

    files = [
        ("files", ("first.txt", b"1234", "text/plain")),
        ("files", ("second.txt", b"123456", "text/plain")),
    ]
    response = test_client.post("/api/knowledge/bases/research/upload", files=files)

    assert response.status_code == 413
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()


def test_knowledge_upload_initializes_manager_without_forcing_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Upload should let shared-manager bootstrap choose its own startup mode."""
    config = _knowledge_config(tmp_path)
    manager = MagicMock()
    manager.index_file = AsyncMock(return_value=True)
    _publish_committed_runtime_config(test_client.app, config)

    with (
        patch(
            "mindroom.api.knowledge.initialize_shared_knowledge_managers",
            new=AsyncMock(return_value={"research": manager}),
        ) as init_managers,
    ):
        response = test_client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", ("note.txt", b"hello", "text/plain"))],
        )

    assert response.status_code == 200
    assert (tmp_path / "note.txt").exists()
    init_managers.assert_awaited_once()
    assert init_managers.await_args.kwargs["reindex_on_create"] is False
    manager.index_file.assert_awaited_once_with("note.txt", upsert=True)


def test_git_knowledge_upload_waits_for_checkout_ready_before_writing(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Git-backed uploads should block until the checkout is ready for direct writes."""
    config = _knowledge_config(tmp_path, with_git=True)
    manager = MagicMock()
    manager.index_file = AsyncMock(return_value=True)
    checkout_ready = False
    _publish_committed_runtime_config(test_client.app, config)

    async def _ensure_git_checkout_ready() -> None:
        nonlocal checkout_ready
        checkout_ready = True

    async def _stream_upload(
        upload: object,
        destination: Path,
        filename: str,
    ) -> None:
        _ = upload, filename
        assert checkout_ready is True
        destination.write_bytes(b"hello")

    manager.ensure_git_checkout_ready = AsyncMock(side_effect=_ensure_git_checkout_ready)

    with (
        patch("mindroom.api.knowledge._ensure_manager", new=AsyncMock(return_value=manager)),
        patch("mindroom.api.knowledge._stream_upload_to_destination", new=AsyncMock(side_effect=_stream_upload)),
    ):
        response = test_client.post(
            "/api/knowledge/bases/research/upload",
            files=[("files", ("note.txt", b"hello", "text/plain"))],
        )

    assert response.status_code == 200
    manager.ensure_git_checkout_ready.assert_awaited_once_with()
    manager.index_file.assert_awaited_once_with("note.txt", upsert=True)


def test_knowledge_delete_initializes_manager_without_forcing_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Delete should let shared-manager bootstrap choose its own startup mode."""
    config = _knowledge_config(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    manager = MagicMock()
    manager.remove_file = AsyncMock(return_value=True)
    _publish_committed_runtime_config(test_client.app, config)

    with (
        patch(
            "mindroom.api.knowledge.initialize_shared_knowledge_managers",
            new=AsyncMock(return_value={"research": manager}),
        ) as init_managers,
    ):
        response = test_client.delete("/api/knowledge/bases/research/files/a.txt")

    assert response.status_code == 200
    assert not target.exists()
    init_managers.assert_awaited_once()
    assert init_managers.await_args.kwargs["reindex_on_create"] is False
    manager.remove_file.assert_awaited_once_with("a.txt")


def test_knowledge_delete_rejects_path_traversal(test_client: TestClient, tmp_path: Path) -> None:
    """Delete endpoint should reject traversal paths."""
    config = _knowledge_config(tmp_path)
    _publish_committed_runtime_config(test_client.app, config)

    response = test_client.delete("/api/knowledge/bases/research/files/..%2Fsecret.txt")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid path"


def test_unknown_knowledge_base_returns_404(test_client: TestClient, tmp_path: Path) -> None:
    """Endpoints should return 404 for unknown knowledge base IDs."""
    config = _knowledge_config(tmp_path, base_id="legal")
    _publish_committed_runtime_config(test_client.app, config)

    response = test_client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_reindex_uses_git_startup_finisher_for_git_bases(test_client: TestClient, tmp_path: Path) -> None:
    """Git-backed bases should delegate direct reindex requests through the startup finisher."""
    config = _knowledge_config(tmp_path, with_git=True)
    manager = MagicMock()
    _publish_committed_runtime_config(test_client.app, config)
    manager.finish_pending_background_git_startup = AsyncMock(
        return_value={"startup_mode": "full_reindex", "indexed_count": 2},
    )
    manager.restore_deferred_shared_runtime = AsyncMock(return_value=None)

    with (
        patch(
            "mindroom.api.knowledge._ensure_manager_for_explicit_reindex",
            new=AsyncMock(return_value=manager),
        ),
    ):
        response = test_client.post("/api/knowledge/bases/research/reindex")

    assert response.status_code == 200
    assert response.json()["indexed_count"] == 2
    manager.finish_pending_background_git_startup.assert_awaited_once_with(force_full_reindex=True)
    manager.restore_deferred_shared_runtime.assert_awaited_once_with()


def test_reindex_finishes_pending_background_startup_for_git_bases(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Git-backed manual reindex should consume deferred startup work instead of duplicating it."""
    config = _knowledge_config(tmp_path, with_git=True)
    manager = MagicMock()
    manager.finish_pending_background_git_startup = AsyncMock(
        return_value={"startup_mode": "full_reindex", "indexed_count": 5},
    )
    manager.sync_git_repository = AsyncMock()
    manager.reindex_all = AsyncMock()
    manager.restore_deferred_shared_runtime = AsyncMock(return_value=None)
    _publish_committed_runtime_config(test_client.app, config)

    with patch("mindroom.api.knowledge._ensure_manager_for_explicit_reindex", new=AsyncMock(return_value=manager)):
        response = test_client.post("/api/knowledge/bases/research/reindex")

    assert response.status_code == 200
    assert response.json()["indexed_count"] == 5
    manager.finish_pending_background_git_startup.assert_awaited_once_with(force_full_reindex=True)
    manager.sync_git_repository.assert_not_awaited()
    manager.reindex_all.assert_not_awaited()
    manager.restore_deferred_shared_runtime.assert_awaited_once_with()


def test_reindex_restores_deferred_shared_runtime_when_git_reindex_fails(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Explicit Git reindex should restore deferred shared runtime even when the reindex fails."""
    config = _knowledge_config(tmp_path, with_git=True)
    manager = MagicMock()
    manager.finish_pending_background_git_startup = AsyncMock(side_effect=RuntimeError("boom"))
    manager.restore_deferred_shared_runtime = AsyncMock(return_value=None)
    _publish_committed_runtime_config(test_client.app, config)

    with (
        patch("mindroom.api.knowledge._ensure_manager_for_explicit_reindex", new=AsyncMock(return_value=manager)),
        pytest.raises(RuntimeError, match="boom"),
    ):
        test_client.post("/api/knowledge/bases/research/reindex")

    manager.finish_pending_background_git_startup.assert_awaited_once_with(force_full_reindex=True)
    manager.restore_deferred_shared_runtime.assert_awaited_once_with()


def test_reindex_cold_local_base_reindexes_only_once(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Cold manual reindex should not do an eager create-time rebuild before the explicit reindex."""
    config = _knowledge_config(tmp_path)
    _publish_committed_runtime_config(test_client.app, config)
    reindex_all = AsyncMock(return_value=4)

    try:
        with (
            patch("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb),
            patch("mindroom.knowledge.manager.Knowledge", _DummyKnowledge),
            patch("mindroom.knowledge.manager.KnowledgeManager.reindex_all", new=reindex_all),
        ):
            response = test_client.post("/api/knowledge/bases/research/reindex")

        assert response.status_code == 200
        assert response.json()["indexed_count"] == 4
        reindex_all.assert_awaited_once_with()
    finally:
        asyncio.run(shutdown_shared_knowledge_managers())


def test_reindex_cold_git_base_syncs_and_reindexes_only_once(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Cold manual Git reindex should perform one explicit sync/rebuild pass, not two."""
    config = _knowledge_config(tmp_path, with_git=True)
    _publish_committed_runtime_config(test_client.app, config)
    sync_git_repository = AsyncMock(return_value={"updated": False, "changed_count": 0, "removed_count": 0})
    reindex_all = AsyncMock(return_value=6)

    try:
        with (
            patch("mindroom.knowledge.manager.ChromaDb", _DummyChromaDb),
            patch("mindroom.knowledge.manager.Knowledge", _DummyKnowledge),
            patch("mindroom.knowledge.manager.KnowledgeManager.sync_git_repository", new=sync_git_repository),
            patch("mindroom.knowledge.manager.KnowledgeManager.reindex_all", new=reindex_all),
        ):
            response = test_client.post("/api/knowledge/bases/research/reindex")

        assert response.status_code == 200
        assert response.json()["indexed_count"] == 6
        sync_git_repository.assert_awaited_once_with(index_changes=False)
        reindex_all.assert_awaited_once_with()
    finally:
        asyncio.run(shutdown_shared_knowledge_managers())


def test_knowledge_routes_return_runtime_validation_errors(test_client: TestClient) -> None:
    """Knowledge routes should surface malformed runtime config as 422, not generic 500s."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    runtime_paths.config_path.write_text("agents:\n  broken: [\n", encoding="utf-8")
    assert main.load_api_config_into_app(runtime_paths, test_client.app) is False

    response = test_client.get("/api/knowledge/bases")

    assert response.status_code == 422
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]


def test_knowledge_routes_use_committed_snapshot_until_reload(test_client: TestClient, tmp_path: Path) -> None:
    """Knowledge routes should ignore newer on-disk edits until a new snapshot is published."""
    config = _knowledge_config(tmp_path / "published")
    _publish_committed_runtime_config(test_client.app, config)
    runtime_paths = main._app_runtime_paths(test_client.app)
    plugin_root = tmp_path / "plugins" / "bad-name"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name": "BadName", "tools_module": null, "skills": []}',
        encoding="utf-8",
    )
    runtime_paths.config_path.write_text(
        (
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-5.4\n"
            "router:\n"
            "  model: default\n"
            "agents: {}\n"
            "knowledge_bases:\n"
            "  changed:\n"
            "    path: ./other\n"
            "    watch: false\n"
            "plugins:\n"
            "  - ./plugins/bad-name\n"
        ),
        encoding="utf-8",
    )

    response = test_client.get("/api/knowledge/bases")

    assert response.status_code == 200
    assert response.json()["bases"][0]["name"] == "research"


def test_knowledge_routes_ignore_unpublished_plugin_drift(test_client: TestClient, tmp_path: Path) -> None:
    """Knowledge routes should keep serving the published snapshot when plugin files drift on disk."""
    runtime_paths = main._app_runtime_paths(test_client.app)
    plugin_root = runtime_paths.config_path.parent / "plugins" / "demo_plugin"
    plugin_root.mkdir(parents=True)
    manifest_path = plugin_root / "mindroom.plugin.json"
    manifest_path.write_text('{"name": "demo_plugin", "skills": []}', encoding="utf-8")
    config = _knowledge_config(tmp_path / "published")
    config.plugins = [PluginEntryConfig(path="./plugins/demo_plugin")]
    _publish_committed_runtime_config(test_client.app, config)
    manifest_path.write_text('{"name": "BadName", "skills": []}', encoding="utf-8")

    response = test_client.get("/api/knowledge/bases")

    assert response.status_code == 200
    assert response.json()["bases"][0]["name"] == "research"


def test_ensure_manager_reloads_when_knowledge_base_path_changes(tmp_path: Path) -> None:
    """The API helper should not reuse a cached manager for an old base path."""
    storage_path = tmp_path / "storage"
    old_path = tmp_path / "old"
    new_path = tmp_path / "new"
    old_path.mkdir(parents=True, exist_ok=True)
    new_path.mkdir(parents=True, exist_ok=True)

    config_old = _knowledge_config(old_path)
    config_new = _knowledge_config(new_path)
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=storage_path)

    async def _run() -> None:
        try:
            await initialize_shared_knowledge_managers(
                config_old,
                runtime_paths,
                start_watchers=False,
                reindex_on_create=False,
            )
            manager = await knowledge_api._ensure_manager(config_new, "research", runtime_paths)
            assert manager is not None
            assert manager.knowledge_path == new_path.resolve()
        finally:
            await shutdown_shared_knowledge_managers()

    asyncio.run(_run())
