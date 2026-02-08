"""Tests for knowledge management API routes."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from mindroom.config import Config, KnowledgeConfig

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from fastapi.testclient import TestClient


def _knowledge_config(path: Path, *, enabled: bool = True) -> Config:
    return Config(
        agents={},
        models={},
        knowledge=KnowledgeConfig(
            enabled=enabled,
            path=str(path),
            watch=False,
        ),
    )


def test_knowledge_status_initializes_manager_without_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Status should initialize manager only in incremental mode."""
    config = _knowledge_config(tmp_path, enabled=True)
    manager = MagicMock()
    manager.get_status.return_value = {"indexed_count": 3, "file_count": 4}

    with (
        patch("mindroom.api.knowledge.Config.from_yaml", return_value=config),
        patch(
            "mindroom.api.knowledge.initialize_knowledge_manager",
            new=AsyncMock(return_value=manager),
        ) as init_manager,
    ):
        response = test_client.get("/api/knowledge/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["indexed_count"] == 3
    assert payload["file_count"] == 4
    init_manager.assert_awaited_once()
    assert init_manager.await_args.kwargs["reindex_on_create"] is False


def test_knowledge_upload_rolls_back_on_oversized_file(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When one file is too large, previous files in the same request are removed."""
    config = _knowledge_config(tmp_path, enabled=False)
    monkeypatch.setattr("mindroom.api.knowledge._MAX_UPLOAD_BYTES", 5)

    files = [
        ("files", ("first.txt", b"1234", "text/plain")),
        ("files", ("second.txt", b"123456", "text/plain")),
    ]

    with patch("mindroom.api.knowledge.Config.from_yaml", return_value=config):
        response = test_client.post("/api/knowledge/upload", files=files)

    assert response.status_code == 413
    assert not (tmp_path / "first.txt").exists()
    assert not (tmp_path / "second.txt").exists()


def test_knowledge_upload_initializes_manager_without_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Upload should create manager in incremental mode to avoid full reindex-on-first-call."""
    config = _knowledge_config(tmp_path, enabled=True)
    manager = MagicMock()
    manager.index_file = AsyncMock(return_value=True)

    with (
        patch("mindroom.api.knowledge.Config.from_yaml", return_value=config),
        patch(
            "mindroom.api.knowledge.initialize_knowledge_manager",
            new=AsyncMock(return_value=manager),
        ) as init_manager,
    ):
        response = test_client.post(
            "/api/knowledge/upload",
            files=[("files", ("note.txt", b"hello", "text/plain"))],
        )

    assert response.status_code == 200
    assert (tmp_path / "note.txt").exists()
    init_manager.assert_awaited_once()
    assert init_manager.await_args.kwargs["reindex_on_create"] is False
    manager.index_file.assert_awaited_once_with("note.txt", upsert=True)


def test_knowledge_delete_initializes_manager_without_full_reindex(
    test_client: TestClient,
    tmp_path: Path,
) -> None:
    """Delete should update vector index without forcing a full reindex."""
    config = _knowledge_config(tmp_path, enabled=True)
    target = tmp_path / "a.txt"
    target.write_text("hello", encoding="utf-8")
    manager = MagicMock()
    manager.remove_file = AsyncMock(return_value=True)

    with (
        patch("mindroom.api.knowledge.Config.from_yaml", return_value=config),
        patch(
            "mindroom.api.knowledge.initialize_knowledge_manager",
            new=AsyncMock(return_value=manager),
        ) as init_manager,
    ):
        response = test_client.delete("/api/knowledge/files/a.txt")

    assert response.status_code == 200
    assert not target.exists()
    init_manager.assert_awaited_once()
    assert init_manager.await_args.kwargs["reindex_on_create"] is False
    manager.remove_file.assert_awaited_once_with("a.txt")


def test_knowledge_delete_rejects_path_traversal(test_client: TestClient, tmp_path: Path) -> None:
    """Delete endpoint should reject traversal paths."""
    config = _knowledge_config(tmp_path, enabled=False)

    with patch("mindroom.api.knowledge.Config.from_yaml", return_value=config):
        response = test_client.delete("/api/knowledge/files/..%2Fsecret.txt")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid path"
