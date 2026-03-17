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


def test_knowledge_bases_list_initializes_managers_with_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Base listing should initialize managers with full create-time indexing."""
    config = _knowledge_config(tmp_path)
    manager = MagicMock()
    manager.get_status.return_value = {"indexed_count": 3, "file_count": 4}
    runtime_paths = main._app_runtime_paths(test_client.app)

    with (
        patch("mindroom.api.knowledge._load_runtime_config", return_value=(config, runtime_paths)),
        patch(
            "mindroom.api.knowledge.initialize_shared_knowledge_managers",
            new=AsyncMock(return_value={"research": manager}),
        ) as init_managers,
    ):
        response = test_client.get("/api/knowledge/bases")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["bases"][0]["name"] == "research"
    assert payload["bases"][0]["indexed_count"] == 3
    assert payload["bases"][0]["file_count"] == 4
    init_managers.assert_awaited_once()
    assert init_managers.await_args.kwargs["reindex_on_create"] is True


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
    runtime_paths = main._app_runtime_paths(test_client.app)

    with (
        patch("mindroom.api.knowledge._load_runtime_config", return_value=(config, runtime_paths)),
        patch(
            "mindroom.api.knowledge.initialize_shared_knowledge_managers",
            new=AsyncMock(return_value={"research": manager}),
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
    init_managers.assert_awaited_once()
    assert init_managers.await_args.kwargs["reindex_on_create"] is True


def test_knowledge_upload_rolls_back_on_oversized_file(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When one file is too large, previous files in the same request are removed."""
    config = _knowledge_config(tmp_path)
    monkeypatch.setattr("mindroom.api.knowledge._MAX_UPLOAD_BYTES", 5)

    files = [
        ("files", ("first.txt", b"1234", "text/plain")),
        ("files", ("second.txt", b"123456", "text/plain")),
    ]
    runtime_paths = main._app_runtime_paths(test_client.app)

    with patch("mindroom.api.knowledge._load_runtime_config", return_value=(config, runtime_paths)):
        response = test_client.post("/api/knowledge/bases/research/upload", files=files)

    assert response.status_code == 413
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()


def test_knowledge_upload_initializes_manager_with_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Upload should initialize managers with full create-time indexing."""
    config = _knowledge_config(tmp_path)
    manager = MagicMock()
    manager.index_file = AsyncMock(return_value=True)
    runtime_paths = main._app_runtime_paths(test_client.app)

    with (
        patch("mindroom.api.knowledge._load_runtime_config", return_value=(config, runtime_paths)),
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
    assert init_managers.await_args.kwargs["reindex_on_create"] is True
    manager.index_file.assert_awaited_once_with("note.txt", upsert=True)


def test_knowledge_delete_initializes_manager_with_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Delete should use managers initialized with full create-time indexing."""
    config = _knowledge_config(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    manager = MagicMock()
    manager.remove_file = AsyncMock(return_value=True)
    runtime_paths = main._app_runtime_paths(test_client.app)

    with (
        patch("mindroom.api.knowledge._load_runtime_config", return_value=(config, runtime_paths)),
        patch(
            "mindroom.api.knowledge.initialize_shared_knowledge_managers",
            new=AsyncMock(return_value={"research": manager}),
        ) as init_managers,
    ):
        response = test_client.delete("/api/knowledge/bases/research/files/a.txt")

    assert response.status_code == 200
    assert not target.exists()
    init_managers.assert_awaited_once()
    assert init_managers.await_args.kwargs["reindex_on_create"] is True
    manager.remove_file.assert_awaited_once_with("a.txt")


def test_knowledge_delete_rejects_path_traversal(test_client: TestClient, tmp_path: Path) -> None:
    """Delete endpoint should reject traversal paths."""
    config = _knowledge_config(tmp_path)
    runtime_paths = main._app_runtime_paths(test_client.app)

    with patch("mindroom.api.knowledge._load_runtime_config", return_value=(config, runtime_paths)):
        response = test_client.delete("/api/knowledge/bases/research/files/..%2Fsecret.txt")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid path"


def test_unknown_knowledge_base_returns_404(test_client: TestClient, tmp_path: Path) -> None:
    """Endpoints should return 404 for unknown knowledge base IDs."""
    config = _knowledge_config(tmp_path, base_id="legal")
    runtime_paths = main._app_runtime_paths(test_client.app)

    with patch("mindroom.api.knowledge._load_runtime_config", return_value=(config, runtime_paths)):
        response = test_client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_reindex_syncs_git_before_reindex_for_git_bases(test_client: TestClient, tmp_path: Path) -> None:
    """Git-backed bases should fetch/sync before a full reindex."""
    config = _knowledge_config(tmp_path, with_git=True)
    manager = MagicMock()
    call_order: list[str] = []
    runtime_paths = main._app_runtime_paths(test_client.app)

    async def _sync() -> dict[str, int | bool]:
        call_order.append("sync")
        return {"updated": True, "changed_count": 0, "removed_count": 0}

    async def _reindex() -> int:
        call_order.append("reindex")
        return 2

    manager.sync_git_repository = AsyncMock(side_effect=_sync)
    manager.reindex_all = AsyncMock(side_effect=_reindex)

    with (
        patch("mindroom.api.knowledge._load_runtime_config", return_value=(config, runtime_paths)),
        patch(
            "mindroom.api.knowledge.initialize_shared_knowledge_managers",
            new=AsyncMock(return_value={"research": manager}),
        ),
    ):
        response = test_client.post("/api/knowledge/bases/research/reindex")

    assert response.status_code == 200
    assert call_order == ["sync", "reindex"]
    manager.sync_git_repository.assert_awaited_once()
    manager.reindex_all.assert_awaited_once()


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
