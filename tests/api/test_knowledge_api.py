"""Tests for knowledge management API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from mindroom.config import Config, KnowledgeBaseConfig

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from fastapi.testclient import TestClient


def _knowledge_config(path: Path, *, base_id: str = "research") -> Config:
    return Config(
        agents={},
        models={},
        knowledge_bases={
            base_id: KnowledgeBaseConfig(
                path=str(path),
                watch=False,
            ),
        },
    )


def test_knowledge_bases_list_initializes_managers_without_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Base listing should initialize managers only in incremental mode."""
    config = _knowledge_config(tmp_path)
    manager = MagicMock()
    manager.get_status.return_value = {"indexed_count": 3, "file_count": 4}

    with (
        patch("mindroom.api.knowledge.Config.from_yaml", return_value=config),
        patch(
            "mindroom.api.knowledge.initialize_knowledge_managers",
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
    assert init_managers.await_args.kwargs["reindex_on_create"] is False


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

    with (
        patch("mindroom.api.knowledge.Config.from_yaml", return_value=config),
        patch(
            "mindroom.api.knowledge.initialize_knowledge_managers",
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
    assert init_managers.await_args.kwargs["reindex_on_create"] is False


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

    with patch("mindroom.api.knowledge.Config.from_yaml", return_value=config):
        response = test_client.post("/api/knowledge/bases/research/upload", files=files)

    assert response.status_code == 413
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()


def test_knowledge_upload_initializes_manager_without_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Upload should create manager in incremental mode to avoid full reindex-on-first-call."""
    config = _knowledge_config(tmp_path)
    manager = MagicMock()
    manager.index_file = AsyncMock(return_value=True)

    with (
        patch("mindroom.api.knowledge.Config.from_yaml", return_value=config),
        patch(
            "mindroom.api.knowledge.initialize_knowledge_managers",
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


def test_knowledge_delete_initializes_manager_without_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Delete should update vector index without forcing a full reindex."""
    config = _knowledge_config(tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    manager = MagicMock()
    manager.remove_file = AsyncMock(return_value=True)

    with (
        patch("mindroom.api.knowledge.Config.from_yaml", return_value=config),
        patch(
            "mindroom.api.knowledge.initialize_knowledge_managers",
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

    with patch("mindroom.api.knowledge.Config.from_yaml", return_value=config):
        response = test_client.delete("/api/knowledge/bases/research/files/..%2Fsecret.txt")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid path"


def test_unknown_knowledge_base_returns_404(test_client: TestClient, tmp_path: Path) -> None:
    """Endpoints should return 404 for unknown knowledge base IDs."""
    config = _knowledge_config(tmp_path, base_id="legal")

    with patch("mindroom.api.knowledge.Config.from_yaml", return_value=config):
        response = test_client.get("/api/knowledge/bases/research/status")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]
