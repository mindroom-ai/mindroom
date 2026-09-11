"""Tests for live response activity reporting."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom import constants, orchestrator
from mindroom.api import config_lifecycle, main
from mindroom.response_admission import ResponseAdmissionGate
from mindroom.response_tracking import ResponseActivityTracker
from mindroom.runtime_state import reset_runtime_state, set_runtime_ready, set_runtime_starting
from tests.api.conftest import trusted_upstream_headers

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
    state.openai_response_tracker = ResponseActivityTracker()
    reset_runtime_state()
    yield
    state.response_admission_gate = None
    state.openai_response_tracker = ResponseActivityTracker()
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
    set_runtime_ready()
    with state.openai_response_tracker.track(responder="helper", requester_id="@alice:example.org"):
        response = test_client.get("/api/responses/activity")
    assert response.status_code == 200
    assert response.json()["status"] == "busy"
    assert response.json()["active_openai_requests"] == 1


def _configure_details_runtime(
    test_client: TestClient,
    temp_config_file: Path,
    *,
    api_key: str | None,
    trusted_upstream: bool = False,
) -> None:
    process_env = {"MINDROOM_OWNER_USER_ID": "@owner:example.org"}
    if api_key is not None:
        process_env["MINDROOM_API_KEY"] = api_key
    if trusted_upstream:
        process_env.update(
            {
                "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED": "true",
                "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER": "X-Trusted-User",
                "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER": "X-Trusted-Email",
                "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER": "X-Trusted-Matrix-User",
            },
        )
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=temp_config_file, process_env=process_env)
    main.initialize_api_app(test_client.app, runtime_paths)


@pytest.mark.parametrize(
    ("api_key", "headers", "status_code"),
    [
        (None, {}, 503),
        ("test-key", {}, 401),
        ("test-key", {"Authorization": "Bearer wrong-key"}, 401),
        ("test-key", {"Authorization": b"Bearer \xff"}, 401),
    ],
)
def test_response_activity_details_requires_configured_matching_key(
    test_client: TestClient,
    temp_config_file: Path,
    api_key: str | None,
    headers: dict[str, str | bytes],
    status_code: int,
) -> None:
    """Detailed identities fail closed without the selected runtime's exact operator key."""
    _configure_details_runtime(test_client, temp_config_file, api_key=api_key)
    response = test_client.get("/api/responses/activity/details", headers=headers)
    assert response.status_code == status_code
    assert "test-key" not in response.text
    assert "wrong-key" not in response.text


def test_response_activity_details_groups_identities_and_reconciles_unknown_slots(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Each channel groups immutable identities and labels admitted slots without metadata as unknown."""
    _configure_details_runtime(test_client, temp_config_file, api_key="test-key")
    state = config_lifecycle.app_state(test_client.app)
    gate = ResponseAdmissionGate()
    state.response_admission_gate = gate
    assert gate.admit()
    assert gate.admit()
    set_runtime_ready()

    with (
        gate.track_response(responder="helper", requester_id="@alice:example.org"),
        state.openai_response_tracker.track(responder="general", requester_id="@alice:example.org"),
        state.openai_response_tracker.track(responder="general", requester_id="@alice:example.org"),
    ):
        response = test_client.get(
            "/api/responses/activity/details",
            headers={"Authorization": "Bearer test-key"},
        )

    gate.release()
    gate.release()
    assert response.status_code == 200
    assert response.json() == {
        "runtime_phase": "ready",
        "admission_paused": False,
        "active_matrix_operations": 2,
        "active_openai_requests": 2,
        "responses": [
            {
                "channel": "matrix",
                "responder": "helper",
                "requester_id": "@alice:example.org",
                "operations": 1,
            },
            {"channel": "matrix", "responder": None, "requester_id": None, "operations": 1},
            {
                "channel": "openai",
                "responder": "general",
                "requester_id": "@alice:example.org",
                "operations": 2,
            },
        ],
        "status": "busy",
    }
    assert response.headers["cache-control"] == "no-store"


def test_response_activity_details_coalesces_tracked_and_untracked_unknown_slots(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """One unknown identity row includes both tracked and metadata-free admitted slots."""
    _configure_details_runtime(test_client, temp_config_file, api_key="test-key")
    state = config_lifecycle.app_state(test_client.app)
    gate = ResponseAdmissionGate()
    state.response_admission_gate = gate
    assert gate.admit()
    assert gate.admit()
    set_runtime_ready()

    with gate.track_response():
        response = test_client.get(
            "/api/responses/activity/details",
            headers={"Authorization": "Bearer test-key"},
        )

    gate.release()
    gate.release()
    assert response.status_code == 200
    assert response.json()["responses"] == [
        {"channel": "matrix", "responder": None, "requester_id": None, "operations": 2},
    ]


def test_response_activity_details_uses_api_key_even_with_trusted_upstream(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Proxy identity neither grants nor blocks direct operator-key access."""
    _configure_details_runtime(test_client, temp_config_file, api_key="test-key", trusted_upstream=True)
    state = config_lifecycle.app_state(test_client.app)
    state.response_admission_gate = ResponseAdmissionGate()
    set_runtime_ready()
    assert (
        test_client.get(
            "/api/responses/activity/details",
            headers=trusted_upstream_headers(),
        ).status_code
        == 401
    )
    assert (
        test_client.get(
            "/api/responses/activity/details",
            headers={"Authorization": "Bearer test-key"},
        ).status_code
        == 200
    )


def test_aggregate_response_activity_never_serializes_identities(test_client: TestClient) -> None:
    """Public aggregate activity remains free of responder and requester metadata."""
    state = config_lifecycle.app_state(test_client.app)
    state.response_admission_gate = ResponseAdmissionGate()
    set_runtime_ready()
    with state.openai_response_tracker.track(responder="helper", requester_id="@alice:example.org"):
        payload = test_client.get("/api/responses/activity").json()
    assert "responses" not in payload
    assert "helper" not in str(payload)
    assert "@alice:example.org" not in str(payload)


def test_recovery_work_blocks_idle(test_client: TestClient) -> None:
    """Delivery recovery counts alongside admitted work without reserving admission."""
    gate = ResponseAdmissionGate()
    config_lifecycle.app_state(main.app).response_admission_gate = gate
    set_runtime_ready()
    with gate.track_recovery():
        response = test_client.get("/api/responses/activity")
        assert response.json()["status"] == "busy"
        assert response.json()["active_matrix_operations"] == 1
        assert gate.admit()
        assert test_client.get("/api/responses/activity").json()["active_matrix_operations"] == 2
        gate.release()
    assert test_client.get("/api/responses/activity").json()["status"] == "idle"


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
