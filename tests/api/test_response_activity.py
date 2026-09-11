"""Tests for live response activity reporting."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom import constants, orchestrator
from mindroom.api import config_lifecycle, main
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.runtime_state import reset_runtime_state, set_runtime_ready, set_runtime_starting

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def reset_activity(test_client: TestClient) -> Iterator[None]:
    """Isolate the live runtime binding and phase between requests."""
    del test_client
    state = config_lifecycle.app_state(main.app)
    state.response_admission_gate = None
    state.active_openai_requests = 0
    reset_runtime_state()
    yield
    state.response_admission_gate = None
    state.active_openai_requests = 0
    reset_runtime_state()


def test_unbound_runtime_cannot_report_idle(test_client: TestClient) -> None:
    """An API process without its orchestrator must fail closed, even when ready."""
    set_runtime_ready()
    response = test_client.get("/api/responses/activity")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["active_matrix_operations"] is None


def test_live_admissions_are_read_on_every_request(test_client: TestClient) -> None:
    """Acquiring and releasing real admission slots must change the reported status."""
    gate = ResponseAdmissionGate()
    config_lifecycle.app_state(main.app).response_admission_gate = gate
    set_runtime_ready()
    response = test_client.get("/api/responses/activity")
    assert response.status_code == 200
    assert response.json() == {
        "runtime_phase": "ready",
        "admission_paused": False,
        "active_matrix_operations": 0,
        "active_openai_requests": 0,
        "status": "idle",
    }
    assert response.headers["cache-control"] == "no-store"
    assert gate.admit()
    assert gate.admit()
    response = test_client.get("/api/responses/activity")
    assert response.json()["status"] == "busy"
    assert response.json()["active_matrix_operations"] == 2
    gate.release()
    assert test_client.get("/api/responses/activity").json()["status"] == "busy"
    gate.release()
    assert test_client.get("/api/responses/activity").json()["status"] == "idle"


@pytest.mark.parametrize("phase", ["starting", "replacement"])
def test_transition_cannot_report_idle(test_client: TestClient, phase: str) -> None:
    """Startup and a closed replacement gate are not trustworthy idle snapshots."""
    gate = ResponseAdmissionGate()
    config_lifecycle.app_state(main.app).response_admission_gate = gate
    if phase == "starting":
        set_runtime_starting()
    else:
        set_runtime_ready()
        assert gate.close_if_idle()
    response = test_client.get("/api/responses/activity")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_openai_request_blocks_idle(test_client: TestClient) -> None:
    """OpenAI requests count even when the Matrix gate has no admitted work."""
    state = config_lifecycle.app_state(main.app)
    state.response_admission_gate = ResponseAdmissionGate()
    state.active_openai_requests = 1
    set_runtime_ready()
    response = test_client.get("/api/responses/activity")
    assert response.status_code == 200
    assert response.json()["status"] == "busy"
    assert response.json()["active_openai_requests"] == 1


@pytest.mark.asyncio
async def test_embedded_api_binds_and_clears_live_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The bundled server must report its orchestrator's gate and unbind on shutdown."""
    gate = ResponseAdmissionGate()
    assert gate.admit()
    shutdown_requested = asyncio.Event()
    shutdown_requested.set()

    async def serve(server: object) -> None:
        del server
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            response = await client.get("/api/responses/activity")
        assert response.status_code == 200
        assert response.json()["active_matrix_operations"] == 1

    monkeypatch.setattr(orchestrator._SignalAwareUvicornServer, "serve", serve)
    set_runtime_ready()
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    await orchestrator._run_api_server(
        "127.0.0.1",
        8765,
        "ERROR",
        runtime_paths,
        shutdown_requested=shutdown_requested,
        response_admission_gate=gate,
    )
    assert config_lifecycle.app_state(main.app).response_admission_gate is None
