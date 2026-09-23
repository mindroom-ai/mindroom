"""Operator reload status comes from the orchestrator, not the API config cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom import constants
from mindroom.api import config_lifecycle, main
from mindroom.config_reload import ConfigReloadStatus
from mindroom.runtime_state import reset_runtime_state, set_runtime_ready, set_runtime_starting

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def runtime(test_client: TestClient, temp_config_file: Path) -> Iterator[None]:
    """Give the operator a configured key and isolate runtime availability."""
    paths = constants.resolve_primary_runtime_paths(
        config_path=temp_config_file,
        process_env={"MINDROOM_API_KEY": "operator-key"},
    )
    main.initialize_api_app(test_client.app, paths)
    reset_runtime_state()
    yield
    config_lifecycle.app_state(main.app).config_reload_status = None
    reset_runtime_state()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_operator_key_required(test_client: TestClient, headers: dict[str, str]) -> None:
    """Reload metadata must not be exposed through an unauthenticated route."""
    response = test_client.get("/api/config/reload-status", headers=headers)
    assert response.status_code == 401


def test_runtime_binding_and_readiness_required(test_client: TestClient) -> None:
    """A populated API cache or startup binding cannot prove completed application."""
    headers = {"Authorization": "Bearer operator-key"}
    set_runtime_ready()
    assert test_client.get("/api/config/reload-status", headers=headers).status_code == 503
    config_lifecycle.app_state(main.app).config_reload_status = lambda: ConfigReloadStatus(
        status="applied",
        fingerprint="a" * 64,
    )
    set_runtime_starting()
    response = test_client.get("/api/config/reload-status", headers=headers)
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_reads_current_runtime_receipt(test_client: TestClient) -> None:
    """Each request sees the lifecycle's latest result without serving cached success."""
    set_runtime_ready()
    status = ConfigReloadStatus(status="pending", fingerprint="a" * 64)
    config_lifecycle.app_state(main.app).config_reload_status = lambda: status
    headers = {"Authorization": "Bearer operator-key"}
    response = test_client.get("/api/config/reload-status", headers=headers)
    assert response.json() == {"status": "pending", "fingerprint": "a" * 64}
    status = ConfigReloadStatus(status="applied", fingerprint="a" * 64)
    response = test_client.get("/api/config/reload-status", headers=headers)
    assert response.json()["status"] == "applied"
    assert response.headers["cache-control"] == "no-store"
