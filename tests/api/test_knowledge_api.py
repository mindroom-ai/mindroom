"""Tests for non-initializing knowledge management API routes."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import main
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _knowledge_config(path: Path, *, extra_base: bool = False) -> Config:
    knowledge_bases = {
        "research": KnowledgeBaseConfig(path=str(path), watch=False),
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
