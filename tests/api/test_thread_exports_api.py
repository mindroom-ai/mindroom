"""Administrative export borrows the selected running installation."""

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.thread_export.models import ThreadExportStats


@pytest.mark.parametrize("mismatch", [None, "config_path", "storage_root"])
def test_export_requires_matching_runtime(test_client: TestClient, tmp_path: Path, mismatch: str | None) -> None:
    """A CLI aimed at another installation cannot write any exports."""
    paths = config_lifecycle.require_api_state(main.app).snapshot.runtime_paths
    payload = {"config_path": str(paths.config_path), "storage_root": str(paths.storage_root)}
    if mismatch:
        payload[mismatch] = str(tmp_path / "another-install")
    runner = Mock()
    runner.export_once = AsyncMock(return_value=ThreadExportStats(output_dir=tmp_path / "exports"))
    config_lifecycle.app_state(main.app).thread_export_runner = runner
    try:
        response = test_client.post("/api/threads/export", json=payload)
        assert response.status_code == (409 if mismatch else 200)
        if mismatch:
            runner.export_once.assert_not_awaited()
        else:
            runner.export_once.assert_awaited_once()
            assert response.json()["output_dir"] == str(tmp_path / "exports")
    finally:
        config_lifecycle.app_state(main.app).thread_export_runner = None


def test_export_requires_running_owner(test_client: TestClient) -> None:
    """An API-only process has no authority to open a second Matrix owner."""
    paths = config_lifecycle.require_api_state(main.app).snapshot.runtime_paths
    config_lifecycle.app_state(main.app).thread_export_runner = None
    response = test_client.post(
        "/api/threads/export",
        json={"config_path": str(paths.config_path), "storage_root": str(paths.storage_root)},
    )
    assert response.status_code == 503
    assert "running" in response.json()["detail"]


def test_export_uses_normal_api_authentication(test_client: TestClient, tmp_path: Path) -> None:
    """A configured API key protects export just like other administrative routes."""
    previous = config_lifecycle.require_api_state(main.app).snapshot.runtime_paths
    paths = constants.resolve_primary_runtime_paths(
        config_path=previous.config_path,
        storage_path=previous.storage_root,
        process_env={"MINDROOM_API_KEY": "export-test-key"},
    )
    main.initialize_api_app(main.app, paths)
    config_lifecycle.load_config_into_app(paths, main.app)
    runner = Mock(export_once=AsyncMock(return_value=ThreadExportStats(output_dir=tmp_path / "out")))
    config_lifecycle.app_state(main.app).thread_export_runner = runner
    body = {"config_path": str(paths.config_path), "storage_root": str(paths.storage_root)}
    try:
        assert test_client.post("/api/threads/export", json=body).status_code == 401
        runner.export_once.assert_not_awaited()
        response = test_client.post(
            "/api/threads/export",
            json=body,
            headers={"Authorization": "Bearer export-test-key"},
        )
        assert response.status_code == 200
    finally:
        main.initialize_api_app(main.app, previous)
