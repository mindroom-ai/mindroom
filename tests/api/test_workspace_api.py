"""Tests for workspace API routes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

from mindroom.config import AgentConfig, Config
from mindroom.workspace import append_daily_log

if TYPE_CHECKING:
    from pathlib import Path

    import pytest
    from fastapi.testclient import TestClient


def _workspace_config(*, max_file_size: int = 16384) -> Config:
    config = Config(
        agents={
            "test_agent": AgentConfig(
                display_name="Test Agent",
                role="test role",
            ),
        },
        models={},
    )
    config.memory.workspace.max_file_size = max_file_size
    return config


def test_workspace_list_files(test_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """List endpoint should return base workspace files."""
    config = _workspace_config()
    monkeypatch.setattr("mindroom.api.workspace.STORAGE_PATH_OBJ", tmp_path)

    with patch("mindroom.api.workspace.Config.from_yaml", return_value=config):
        response = test_client.get("/api/workspace/test_agent/files")

    assert response.status_code == 200
    payload = response.json()
    filenames = {item["filename"] for item in payload["files"]}
    assert "SOUL.md" in filenames
    assert "AGENTS.md" in filenames
    assert "MEMORY.md" in filenames


def test_workspace_read_file_sets_etag(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read endpoint should include ETag and content payload."""
    config = _workspace_config()
    monkeypatch.setattr("mindroom.api.workspace.STORAGE_PATH_OBJ", tmp_path)

    with patch("mindroom.api.workspace.Config.from_yaml", return_value=config):
        response = test_client.get("/api/workspace/test_agent/file/SOUL.md")

    assert response.status_code == 200
    assert response.headers.get("etag")
    payload = response.json()
    assert payload["filename"] == "SOUL.md"
    assert "# SOUL.md" in payload["content"]


def test_workspace_update_requires_if_match(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT should require If-Match header."""
    config = _workspace_config()
    monkeypatch.setattr("mindroom.api.workspace.STORAGE_PATH_OBJ", tmp_path)

    with patch("mindroom.api.workspace.Config.from_yaml", return_value=config):
        response = test_client.put(
            "/api/workspace/test_agent/file/SOUL.md",
            json={"content": "updated"},
        )

    assert response.status_code == 428


def test_workspace_update_rejects_stale_etag(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT should fail with 409 when If-Match doesn't match current ETag."""
    config = _workspace_config()
    monkeypatch.setattr("mindroom.api.workspace.STORAGE_PATH_OBJ", tmp_path)

    with patch("mindroom.api.workspace.Config.from_yaml", return_value=config):
        read_response = test_client.get("/api/workspace/test_agent/file/SOUL.md")
        stale_etag = read_response.headers["etag"]

        update_response = test_client.put(
            "/api/workspace/test_agent/file/SOUL.md",
            json={"content": "new content"},
            headers={"If-Match": stale_etag},
        )
        assert update_response.status_code == 200

        stale_response = test_client.put(
            "/api/workspace/test_agent/file/SOUL.md",
            json={"content": "another content"},
            headers={"If-Match": stale_etag},
        )

    assert stale_response.status_code == 409


def test_workspace_update_rejects_oversized_content(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUT should return 400 for content above max file size."""
    config = _workspace_config(max_file_size=10)
    monkeypatch.setattr("mindroom.api.workspace.STORAGE_PATH_OBJ", tmp_path)

    with patch("mindroom.api.workspace.Config.from_yaml", return_value=config):
        read_response = test_client.get("/api/workspace/test_agent/file/SOUL.md")
        etag = read_response.headers["etag"]
        response = test_client.put(
            "/api/workspace/test_agent/file/SOUL.md",
            json={"content": "this is definitely too long"},
            headers={"If-Match": etag},
        )

    assert response.status_code == 400
    assert "max file size" in response.json()["detail"]


def test_workspace_allowlist_and_path_traversal(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File endpoint should reject non-allowlisted and traversal paths."""
    config = _workspace_config()
    monkeypatch.setattr("mindroom.api.workspace.STORAGE_PATH_OBJ", tmp_path)

    with patch("mindroom.api.workspace.Config.from_yaml", return_value=config):
        disallowed = test_client.get("/api/workspace/test_agent/file/not_allowed.md")
        traversal = test_client.get("/api/workspace/test_agent/file/..%2Fsecret.md")
        nested_traversal = test_client.get("/api/workspace/test_agent/file/memory/..%2Fsecret.md")
        dot_segment = test_client.get("/api/workspace/test_agent/file/memory/%2e/secret.md")

    assert disallowed.status_code == 422
    assert traversal.status_code == 422
    assert nested_traversal.status_code == 422
    assert dot_segment.status_code == 422


def test_workspace_daily_endpoints(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daily endpoints should list and read logs written to workspace memory."""
    config = _workspace_config()
    monkeypatch.setattr("mindroom.api.workspace.STORAGE_PATH_OBJ", tmp_path)

    append_daily_log(
        "test_agent",
        tmp_path,
        config,
        "daily note",
        room_id="!room:server",
    )
    today = datetime.now(UTC).date().isoformat()

    with patch("mindroom.api.workspace.Config.from_yaml", return_value=config):
        list_response = test_client.get("/api/workspace/test_agent/memory/daily")
        date_response = test_client.get(f"/api/workspace/test_agent/memory/daily/{today}")

    assert list_response.status_code == 200
    list_payload = list_response.json()
    assert list_payload["count"] == 1
    assert list_payload["files"][0]["filename"].endswith(f"{today}.md")

    assert date_response.status_code == 200
    date_payload = date_response.json()
    assert date_payload["count"] == 1
    assert "daily note" in date_payload["entries"][0]["content"]


def test_workspace_delete_is_durable(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleted base files should stay deleted until explicitly recreated."""
    config = _workspace_config()
    monkeypatch.setattr("mindroom.api.workspace.STORAGE_PATH_OBJ", tmp_path)

    with patch("mindroom.api.workspace.Config.from_yaml", return_value=config):
        delete_response = test_client.delete("/api/workspace/test_agent/file/SOUL.md")
        list_response = test_client.get("/api/workspace/test_agent/files")
        read_response = test_client.get("/api/workspace/test_agent/file/SOUL.md")

    assert delete_response.status_code == 200
    filenames = {entry["filename"] for entry in list_response.json()["files"]}
    assert "SOUL.md" not in filenames
    assert read_response.status_code == 404


def test_workspace_memory_file_listed_files_are_readable(
    test_client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any memory markdown file returned by list should be readable via file endpoint."""
    config = _workspace_config()
    monkeypatch.setattr("mindroom.api.workspace.STORAGE_PATH_OBJ", tmp_path)
    memory_file = tmp_path / "workspace" / "test_agent" / "memory" / "custom.md"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("custom memory", encoding="utf-8")

    with patch("mindroom.api.workspace.Config.from_yaml", return_value=config):
        list_response = test_client.get("/api/workspace/test_agent/files")
        read_response = test_client.get("/api/workspace/test_agent/file/memory/custom.md")

    assert list_response.status_code == 200
    filenames = {entry["filename"] for entry in list_response.json()["files"]}
    assert "memory/custom.md" in filenames
    assert read_response.status_code == 200
    assert read_response.json()["content"] == "custom memory"


def test_agent_context_report_endpoint(test_client: TestClient) -> None:
    """Context report endpoint should return observability payload."""
    response = test_client.get("/api/agents/test_agent/context-report?is_dm=true")
    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_name"] == "test_agent"
    assert payload["is_dm"] is True
    assert "loaded_files" in payload
    assert "warnings" in payload


def test_agent_context_report_unknown_agent_returns_404(test_client: TestClient) -> None:
    """Unknown agents should return 404 on context-report endpoint."""
    response = test_client.get("/api/agents/unknown/context-report")
    assert response.status_code == 404
