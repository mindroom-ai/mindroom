"""Tests for live response activity reporting."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom import constants, orchestrator
from mindroom.api import config_lifecycle, main
from mindroom.response_activity import ActiveScriptRunInfo, ResponseIdentity
from mindroom.response_admission import ResponseAdmissionGate
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
    _unbind_runtime(state)
    yield
    _unbind_runtime(state)


def _unbind_runtime(state: config_lifecycle._MindroomAppState) -> None:
    state.response_admission_gate = None
    state.active_calls = None
    state.active_script_runs = None
    state.openai_responses.clear()
    reset_runtime_state()


def _bind_runtime(
    state: config_lifecycle._MindroomAppState,
    *,
    calls: list[ResponseIdentity] | None = None,
    script_runs: list[ActiveScriptRunInfo] | None = None,
) -> ResponseAdmissionGate:
    """Bind the live sources the embedded orchestrator publishes to its API."""
    gate = ResponseAdmissionGate()
    state.response_admission_gate = gate
    state.active_calls = lambda: list(calls or [])

    async def active_script_runs() -> list[ActiveScriptRunInfo]:
        return list(script_runs or [])

    state.active_script_runs = active_script_runs
    return gate


def _script_run(run_id: str, *, recoverable: bool) -> ActiveScriptRunInfo:
    return ActiveScriptRunInfo(
        run_id=run_id,
        responder="watcher",
        requester_id="@alice:example.org",
        recoverable=recoverable,
    )


def test_unbound_runtime_cannot_report_idle(test_client: TestClient) -> None:
    """An API process without its orchestrator must fail closed, even when ready."""
    set_runtime_ready()
    response = test_client.get("/api/responses/activity")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["active_matrix_operations"] is None


def test_live_admissions_are_read_on_every_request(test_client: TestClient) -> None:
    """Acquiring and releasing real admission slots must change the reported status."""
    gate = _bind_runtime(config_lifecycle.app_state(main.app))
    set_runtime_ready()
    response = test_client.get("/api/responses/activity")
    assert response.status_code == 200
    assert response.json() == {
        "runtime_phase": "ready",
        "admission_paused": False,
        "active_matrix_operations": 0,
        "active_openai_requests": 0,
        "active_calls": 0,
        "interruptible_script_runs": 0,
        "recoverable_script_runs": 0,
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
    gate = _bind_runtime(config_lifecycle.app_state(main.app))
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
    _bind_runtime(state)
    set_runtime_ready()
    state.openai_responses.add(ResponseIdentity("helper", "@alice:example.org"))
    response = test_client.get("/api/responses/activity")
    assert response.status_code == 200
    assert response.json()["status"] == "busy"
    assert response.json()["active_openai_requests"] == 1


def test_active_call_blocks_idle(test_client: TestClient) -> None:
    """A joined voice call is live work that a restart would end."""
    state = config_lifecycle.app_state(main.app)
    _bind_runtime(state, calls=[ResponseIdentity("helper", "@alice:example.org")])
    set_runtime_ready()
    response = test_client.get("/api/responses/activity")
    assert response.status_code == 200
    assert response.json()["status"] == "busy"
    assert response.json()["active_calls"] == 1


@pytest.mark.parametrize(
    ("recoverable", "status", "interruptible", "preserved"),
    [(False, "busy", 1, 0), (True, "idle", 0, 1)],
)
def test_only_restart_interrupted_script_runs_block_idle(
    test_client: TestClient,
    recoverable: bool,
    status: str,
    interruptible: int,
    preserved: int,
) -> None:
    """Runs a restart would adopt are reported without making the runtime busy."""
    state = config_lifecycle.app_state(main.app)
    _bind_runtime(state, script_runs=[_script_run("script-1", recoverable=recoverable)])
    set_runtime_ready()
    payload = test_client.get("/api/responses/activity").json()
    assert payload["status"] == status
    assert payload["interruptible_script_runs"] == interruptible
    assert payload["recoverable_script_runs"] == preserved


@pytest.mark.parametrize("source", ["active_calls", "active_script_runs"])
def test_unbound_call_or_script_source_cannot_report_idle(test_client: TestClient, source: str) -> None:
    """Missing call or script observation fails closed instead of counting as zero."""
    state = config_lifecycle.app_state(main.app)
    _bind_runtime(state)
    setattr(state, source, None)
    set_runtime_ready()
    response = test_client.get("/api/responses/activity")
    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


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


def test_response_activity_details_lists_responses_without_padding_admission_slots(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Details list each response once, independently of nested admission counts."""
    _configure_details_runtime(test_client, temp_config_file, api_key="test-key")
    state = config_lifecycle.app_state(test_client.app)
    gate = _bind_runtime(state)
    assert gate.admit()
    assert gate.admit()
    set_runtime_ready()

    gate.response_identities.add(ResponseIdentity("helper", "@alice:example.org"))
    state.openai_responses.update(
        [ResponseIdentity("general", "@alice:example.org"), ResponseIdentity("general", "@alice:example.org")],
    )
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
        "active_calls": 0,
        "interruptible_script_runs": 0,
        "recoverable_script_runs": 0,
        "responses": [
            {
                "channel": "matrix",
                "responder": "helper",
                "requester_id": "@alice:example.org",
            },
            {
                "channel": "openai",
                "responder": "general",
                "requester_id": "@alice:example.org",
            },
            {
                "channel": "openai",
                "responder": "general",
                "requester_id": "@alice:example.org",
            },
        ],
        "script_runs": [],
        "status": "busy",
    }
    assert response.headers["cache-control"] == "no-store"


def test_response_activity_details_lists_calls_and_script_runs(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Details name each call participant and each unfinished script run with its restart outcome."""
    _configure_details_runtime(test_client, temp_config_file, api_key="test-key")
    state = config_lifecycle.app_state(test_client.app)
    _bind_runtime(
        state,
        calls=[ResponseIdentity("helper", "@bob:example.org")],
        script_runs=[_script_run("script-1", recoverable=False), _script_run("script-2", recoverable=True)],
    )
    set_runtime_ready()
    payload = test_client.get(
        "/api/responses/activity/details",
        headers={"Authorization": "Bearer test-key"},
    ).json()
    assert payload["status"] == "busy"
    assert payload["active_calls"] == 1
    assert payload["interruptible_script_runs"] == 1
    assert payload["recoverable_script_runs"] == 1
    assert payload["responses"] == [{"channel": "call", "responder": "helper", "requester_id": "@bob:example.org"}]
    assert payload["script_runs"] == [
        {"run_id": "script-1", "responder": "watcher", "requester_id": "@alice:example.org", "recoverable": False},
        {"run_id": "script-2", "responder": "watcher", "requester_id": "@alice:example.org", "recoverable": True},
    ]


def test_response_activity_details_uses_api_key_even_with_trusted_upstream(
    test_client: TestClient,
    temp_config_file: Path,
) -> None:
    """Proxy identity neither grants nor blocks direct operator-key access."""
    _configure_details_runtime(test_client, temp_config_file, api_key="test-key", trusted_upstream=True)
    state = config_lifecycle.app_state(test_client.app)
    _bind_runtime(state)
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
    """Public aggregate activity remains free of responder, requester, and run metadata."""
    state = config_lifecycle.app_state(test_client.app)
    _bind_runtime(
        state,
        calls=[ResponseIdentity("helper", "@alice:example.org")],
        script_runs=[_script_run("script-1", recoverable=False)],
    )
    set_runtime_ready()
    state.openai_responses.add(ResponseIdentity("helper", "@alice:example.org"))
    payload = test_client.get("/api/responses/activity").json()
    assert "responses" not in payload
    assert "script_runs" not in payload
    assert "helper" not in str(payload)
    assert "watcher" not in str(payload)
    assert "script-1" not in str(payload)
    assert "@alice:example.org" not in str(payload)


@pytest.mark.asyncio
async def test_embedded_api_binds_and_clears_live_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The bundled server must report its orchestrator's gate, calls, and scripts, then unbind on shutdown."""
    runtime_paths = constants.resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    live = orchestrator._MultiAgentOrchestrator(runtime_paths=runtime_paths)
    live.agent_bots["helper"] = SimpleNamespace(active_call_requesters=("@alice:example.org",))
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
        assert response.json()["active_calls"] == 1
        assert response.json()["interruptible_script_runs"] == 0
        assert response.json()["recoverable_script_runs"] == 0

    monkeypatch.setattr(orchestrator._SignalAwareUvicornServer, "serve", serve)
    set_runtime_ready()
    await orchestrator._run_api_server(
        "127.0.0.1",
        8765,
        "ERROR",
        runtime_paths,
        script_runtime=live.script_runtime,
        shutdown_requested=shutdown_requested,
        response_admission_gate=gate,
        active_calls=live.active_call_identities,
    )
    state = config_lifecycle.app_state(main.app)
    assert state.response_admission_gate is None
    assert state.active_calls is None
    assert state.active_script_runs is None
