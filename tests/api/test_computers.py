"""Public Computer routes deny unauthenticated callers independently of dashboard auth."""

import asyncio
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import httpx
import pytest
import yaml
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from nio.exceptions import LocalProtocolError, RemoteTransportError
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api import computers, config_lifecycle
from mindroom.api import main as api_main
from mindroom.api.computers import router
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.orchestration.computer_runtime import ComputerRuntimeCoordinator
from mindroom.worker_computer.sessions import ComputerError, ComputerSessionStore
from mindroom.workers.backend import WorkerBackend
from mindroom.workers.models import WorkerMaintenanceResult
from tests.computer_helpers import ComputerPeer, authorized_target, computer_app

type Gateway = tuple[TestClient, ComputerPeer, FastAPI]


def test_public_control_requires_session_bearer_and_returns_no_store() -> None:
    """Public control requires session bearer and returns no store."""
    app = FastAPI()
    config_lifecycle.ensure_app_state(app)
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/computers/sessions/missing/control", json={"action": "take"})
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"


@pytest.fixture
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Gateway]:
    """Gateway."""
    peer = ComputerPeer()
    app = computer_app(peer, tmp_path)
    monkeypatch.setattr(computers, "_resolve_worker", lambda *_args, **_kwargs: peer.handle)
    with TestClient(app) as client:
        yield client, peer, app


def create(client: TestClient) -> httpx.Response:
    """Create."""
    return client.post(
        "/api/computers/sessions",
        json={
            "openid_token": {
                "access_token": "openid-secret",
                "token_type": "Bearer",
                "matrix_server_name": "example.org",
                "expires_in": 60,
            },
            "room_id": "!room:example.org",
            "agent_user_id": "@agent:example.org",
        },
    )


def test_public_session_lifecycle_and_scope_revocation(gateway: Gateway) -> None:
    """Public session lifecycle and scope revocation."""
    client, peer, _app = gateway
    response = create(client)
    assert response.status_code == 200, response.text
    session = response.json()
    assert set(session) == {"session_id", "session_token", "state", "mode", "expires_at"}
    assert session["mode"] == "view"
    path = "/api/computers/sessions/" + session["session_id"]
    headers = {"Authorization": "Bearer " + session["session_token"]}
    assert client.get(path).status_code == 401
    assert client.post(path + "/control", headers=headers, json={"action": "take"}).status_code == 409
    status = client.get(path, headers=headers)
    assert "session_token" not in status.json()
    assert status.headers["cache-control"] == "no-store"
    assert "worker-secret" not in status.text
    assert create(client).status_code == 200
    assert create(client).status_code == 429
    peer.allowed = False
    assert client.get(path, headers=headers).status_code == 403
    assert client.get(path, headers=headers).status_code == 401
    assert all("secret" not in url for url, _ in peer.requests)
    assert all(header == "worker-secret" for _, header in peer.requests)


@pytest.mark.parametrize(
    "failure",
    [
        aiohttp.ClientConnectionError,
        aiohttp.ClientPayloadError,
        LocalProtocolError,
        RemoteTransportError,
        OSError,
        TimeoutError,
        None,
    ],
)
@pytest.mark.parametrize("phase", ["create", "status", "upgrade"])
def test_authorization_transport_failure_is_sanitized_and_revokes_session(
    gateway: Gateway,
    failure: type[Exception] | None,
    phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Expected transport outages never escape as framework errors or retain failed capabilities."""
    client, _, app = gateway
    session = create(client).json()
    path = "/api/computers/sessions/" + session["session_id"]
    headers = {"Authorization": "Bearer " + session["session_token"]}
    ticket = client.post(path + "/stream-ticket", headers=headers).json()["ticket"]
    state = config_lifecycle.app_state(app)
    assert state.computer_runtime is not None
    state.computer_runtime = (
        None
        if failure is None
        else replace(
            state.computer_runtime,
            authorize=AsyncMock(
                side_effect=failure("https://matrix.example.org/members?access_token=transport-secret"),
            ),
        )
    )
    if phase == "upgrade":
        with (
            pytest.raises(WebSocketDenialResponse) as denied,
            client.websocket_connect(
                path + "/stream",
                subprotocols=["binary", "mindroom-ticket." + ticket],
                headers={"Origin": "https://chat.example.org"},
            ),
        ):
            pass
        response = denied.value
    else:
        response = create(client) if phase == "create" else client.get(path, headers=headers)
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert set(response.json()) == {"detail"}
    assert len(response.json()["detail"]) < 200
    assert "transport-secret" not in response.text + caplog.text
    if phase != "create":
        assert client.get(path, headers=headers).status_code == 401


def test_active_stream_authorization_transport_failure_revokes_control(
    gateway: Gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later Matrix outage closes the active controller and revokes its bearer."""
    client, _, app = gateway
    monkeypatch.setattr(computers, "_STREAM_RECHECK_SECONDS", 0.01)
    session = create(client).json()
    path = "/api/computers/sessions/" + session["session_id"]
    headers = {"Authorization": "Bearer " + session["session_token"]}
    ticket = client.post(path + "/stream-ticket", headers=headers).json()["ticket"]
    with client.websocket_connect(
        path + "/stream",
        subprotocols=["binary", "mindroom-ticket." + ticket],
        headers={"Origin": "https://chat.example.org"},
    ) as websocket:
        assert websocket.receive_bytes() == b"screen"
        assert client.post(path + "/control", headers=headers, json={"action": "take"}).json()["mode"] == "control"
        state = config_lifecycle.app_state(app)
        assert state.computer_runtime is not None
        state.computer_runtime = replace(
            state.computer_runtime,
            authorize=AsyncMock(side_effect=aiohttp.ServerDisconnectedError("transport-secret")),
        )
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_bytes()
    assert client.get(path, headers=headers).status_code == 401


def test_stream_watch_take_release_reconnect_and_stop(gateway: Gateway) -> None:
    """Stream watch take release reconnect and stop."""
    client, _peer, _app = gateway
    session = create(client).json()
    path = "/api/computers/sessions/" + session["session_id"]
    headers = {"Authorization": "Bearer " + session["session_token"]}

    def protocols() -> list[str]:
        ticket = client.post(path + "/stream-ticket", headers=headers).json()["ticket"]
        return ["binary", "mindroom-ticket." + ticket]

    initial = protocols()
    with client.websocket_connect(
        path + "/stream",
        subprotocols=initial,
        headers={"Origin": "https://chat.example.org"},
    ) as ws:
        assert ws.accepted_subprotocol == "binary"
        assert ws.receive_bytes() == b"screen"
        ws.send_bytes(b"input")
        assert ws.receive_bytes() == b"view"
        assert client.post(path + "/control", headers=headers, json={"action": "take"}).json()["mode"] == "control"
        ws.send_bytes(b"input")
        assert ws.receive_bytes() == b"control"
        assert client.post(path + "/control", headers=headers, json={"action": "release"}).json()["mode"] == "view"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    with (
        pytest.raises(WebSocketDenialResponse),
        client.websocket_connect(
            path + "/stream",
            subprotocols=initial,
            headers={"Origin": "https://chat.example.org"},
        ),
    ):
        pass
    with client.websocket_connect(
        path + "/stream",
        subprotocols=protocols(),
        headers={"Origin": "https://chat.example.org"},
    ) as ws:
        assert ws.receive_bytes() == b"screen"
        assert client.post(path + "/control", headers=headers, json={"action": "stop"}).json()["state"] == "stopped"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    assert client.get(path, headers=headers).status_code == 401
    assert create(client).json()["state"] == "ready"


@pytest.mark.parametrize("failure", ["origin", "missing", "expired", "generation", "config"])
def test_stream_fails_closed_before_upgrade(gateway: Gateway, failure: str) -> None:
    """Stream fails closed before upgrade."""
    client, peer, app = gateway
    session = create(client).json()
    path = "/api/computers/sessions/" + session["session_id"]
    headers = {"Authorization": "Bearer " + session["session_token"]}
    ticket = client.post(path + "/stream-ticket", headers=headers).json()["ticket"]
    protocols = ["binary", "mindroom-ticket." + ticket]
    origin = "https://chat.example.org"
    if failure == "origin":
        origin = "https://evil.example.org"
    elif failure == "missing":
        protocols = ["binary"]
    elif failure == "expired":
        peer.now += 31
    elif failure == "generation":
        state = config_lifecycle.app_state(app)
        state.computer_sessions.get(session["session_id"]).generation = "stale"
    else:
        config_lifecycle.require_api_state(app).snapshot.generation += 1
    with (
        pytest.raises(WebSocketDenialResponse),
        client.websocket_connect(path + "/stream", subprotocols=protocols, headers={"Origin": origin}),
    ):
        pass


def test_malformed_openid_does_not_echo_secret(gateway: Gateway) -> None:
    """Malformed openid does not echo secret."""
    client, _, _ = gateway
    response = client.post("/api/computers/sessions", json={"openid_token": {"access_token": "do-not-echo"}})
    assert response.status_code == 401
    assert "do-not-echo" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_computer_cors_uses_exact_origins_and_no_store(gateway: Gateway) -> None:
    """Dashboard wildcard CORS cannot grant a computer browser origin."""
    client, _, _ = gateway
    for origin, expected in [("https://chat.example.org", 200), ("https://evil.example.org", 400)]:
        response = client.options(
            "/api/computers/sessions",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert response.status_code == expected
        assert response.headers["cache-control"] == "no-store"
        assert response.headers.get("access-control-allow-origin") == (origin if expected == 200 else None)


def test_delete_and_expiry_close_controlling_stream(gateway: Gateway) -> None:
    """Capability revocation closes the worker connection and releases ownership."""
    client, peer, _ = gateway
    session = create(client).json()
    path = "/api/computers/sessions/" + session["session_id"]
    headers = {"Authorization": "Bearer " + session["session_token"]}
    ticket = client.post(path + "/stream-ticket", headers=headers).json()["ticket"]
    with client.websocket_connect(
        path + "/stream",
        subprotocols=["binary", "mindroom-ticket." + ticket],
        headers={"Origin": "https://chat.example.org"},
    ) as websocket:
        assert websocket.receive_bytes() == b"screen"
        assert client.post(path + "/control", headers=headers, json={"action": "take"}).json()["mode"] == "control"
        peer.now += 3600
        assert client.get(path, headers=headers).status_code == 401
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_bytes()
    next_session = create(client).json()
    path = "/api/computers/sessions/" + next_session["session_id"]
    headers = {"Authorization": "Bearer " + next_session["session_token"]}
    assert client.delete(path, headers=headers).status_code == 204
    assert client.get(path, headers=headers).status_code == 401


@pytest.mark.parametrize(
    ("status", "subject", "expected"),
    [
        (401, "@alice:example.org", 401),
        (503, "@alice:example.org", 503),
        (302, "@alice:example.org", 401),
        (200, "@alice:other.org", 401),
        (200, "invalid", 401),
    ],
)
def test_openid_subject_server_and_verifier_failures(
    gateway: Gateway,
    status: int,
    subject: str,
    expected: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reject invalid subjects, redirects and failures without logging verifier credentials."""
    client, peer, _ = gateway
    peer.openid_status = status
    peer.openid_subject = subject
    response = create(client)
    assert response.status_code == expected
    assert "openid-secret" not in response.text
    assert "openid-secret" not in caplog.text


def test_changed_config_conflicts_with_existing_session(gateway: Gateway) -> None:
    """An existing bearer cannot survive an API configuration generation change."""
    client, _, app = gateway
    session = create(client).json()
    config_lifecycle.require_api_state(app).snapshot.generation += 1
    response = client.get(
        "/api/computers/sessions/" + session["session_id"],
        headers={"Authorization": "Bearer " + session["session_token"]},
    )
    assert response.status_code == 409


def test_periodic_authorization_revocation_disconnects_controller(
    gateway: Gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream rechecks policy even while no HTTP controls are requested."""
    client, peer, _ = gateway
    monkeypatch.setattr(computers, "_STREAM_RECHECK_SECONDS", 0.01)
    session = create(client).json()
    path = "/api/computers/sessions/" + session["session_id"]
    headers = {"Authorization": "Bearer " + session["session_token"]}
    ticket = client.post(path + "/stream-ticket", headers=headers).json()["ticket"]
    with client.websocket_connect(
        path + "/stream",
        subprotocols=["binary", "mindroom-ticket." + ticket],
        headers={"Origin": "https://chat.example.org"},
    ) as websocket:
        assert websocket.receive_bytes() == b"screen"
        assert client.post(path + "/control", headers=headers, json={"action": "take"}).json()["mode"] == "control"
        peer.allowed = False
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_bytes()
    assert client.get(path, headers=headers).status_code == 401


def test_missing_stream_ticket_denies_upgrade_with_401(gateway: Gateway) -> None:
    """Websocket authentication failures retain the public HTTP error contract."""
    client, _, _ = gateway
    with (
        pytest.raises(WebSocketDenialResponse) as error,
        client.websocket_connect(
            "/api/computers/sessions/missing/stream",
            subprotocols=["binary"],
            headers={"Origin": "https://chat.example.org"},
        ),
    ):
        pass
    assert error.value.status_code == 401
    assert error.value.headers["cache-control"] == "no-store"


def test_prebound_computer_authorizer_survives_initial_api_config_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initial API startup must retain the orchestrator's already-bound authorization."""
    config = Config()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_WORKER_COMPUTER_ENABLED": "1"},
    )
    app = FastAPI(lifespan=api_main._lifespan)
    app.include_router(router)
    api_main.initialize_api_app(app, paths)
    state = config_lifecycle.app_state(app)
    state.computer_runtime = computers.ComputerRuntime(
        AsyncMock(side_effect=ComputerError(403, "Policy denied.")),
        0,
        config,
    )
    monkeypatch.setattr(computers, "verify_openid", AsyncMock(return_value="@alice:example.org"))
    with TestClient(app) as client:
        loaded = config_lifecycle.require_api_state(app).snapshot.runtime_config
        assert loaded is not None
        assert config.model_dump() == loaded.model_dump()
        assert create(client).status_code == 403


def test_worker_maintenance_touches_computer_stream_before_cleanup(
    gateway: Gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing cleanup seam protects workers with a live viewer stream."""
    client, _, app = gateway
    session = create(client).json()
    path = "/api/computers/sessions/" + session["session_id"]
    headers = {"Authorization": "Bearer " + session["session_token"]}
    ticket = client.post(path + "/stream-ticket", headers=headers).json()["ticket"]
    actions: list[str] = []
    manager = MagicMock(spec=WorkerBackend)
    manager.touch_worker.side_effect = lambda worker_key: actions.append("touch:" + worker_key)
    monkeypatch.setattr(
        api_main,
        "lease_configured_primary_worker_manager",
        lambda *_args, **_kwargs: nullcontext(manager),
    )
    monkeypatch.setattr(
        api_main,
        "maintain_workers",
        lambda _manager: actions.append("maintain") or WorkerMaintenanceResult((), ()),
    )
    with client.websocket_connect(
        path + "/stream",
        subprotocols=["binary", "mindroom-ticket." + ticket],
        headers={"Origin": "https://chat.example.org"},
    ) as websocket:
        assert websocket.receive_bytes() == b"screen"
        config, paths = config_lifecycle.read_app_committed_runtime_config(app)
        api_main._cleanup_workers_once(paths, runtime_config=config, api_app=app)
        assert actions == ["touch:worker", "maintain"]


def test_orchestrator_unbind_revokes_active_stream(gateway: Gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    """Orchestrator shutdown and entity reload revoke viewer credentials immediately."""
    client, _, app = gateway
    monkeypatch.setattr(api_main, "app", app)
    session = create(client).json()
    path = "/api/computers/sessions/" + session["session_id"]
    headers = {"Authorization": "Bearer " + session["session_token"]}
    ticket = client.post(path + "/stream-ticket", headers=headers).json()["ticket"]
    paths = config_lifecycle.require_api_state(app).snapshot.runtime_paths
    coordinator = ComputerRuntimeCoordinator(paths, AgentReplyMembershipIndex())
    with client.websocket_connect(
        path + "/stream",
        subprotocols=["binary", "mindroom-ticket." + ticket],
        headers={"Origin": "https://chat.example.org"},
    ) as websocket:
        assert websocket.receive_bytes() == b"screen"
        assert client.portal is not None
        client.portal.call(coordinator.unbind)
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_bytes()
    assert client.get(path, headers=headers).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_phase", ["authorization", "manager", "status"])
async def test_stream_maintenance_bounds_complete_checks_with_virtual_time(
    monkeypatch: pytest.MonkeyPatch,
    slow_phase: str,
) -> None:
    """A slow successful check fits, but a later overdue phase revokes at 30 seconds."""
    loop = asyncio.get_running_loop()
    now = 0.0
    monkeypatch.setattr(loop, "time", lambda: now)
    app = FastAPI()
    store = ComputerSessionStore(clock=lambda: now)
    config_lifecycle.ensure_app_state(app).computer_sessions = store
    session = store.create(authorized_target())
    stream_closed = asyncio.Event()
    session.stream = stream_closed
    websocket = WebSocket({"type": "websocket", "app": app}, receive=AsyncMock(), send=AsyncMock())
    starts: list[float] = []
    completed: list[float] = []
    cancelled: list[str] = []

    async def checked_status(*_args: object) -> None:
        starts.append(now)
        for phase in ("authorization", "manager", "status"):
            if phase == slow_phase and len(starts) > 1:
                try:
                    await asyncio.sleep(4 if len(starts) == 2 else 18)
                except asyncio.CancelledError:
                    cancelled.append(phase)
                    raise
        completed.append(now)

    monkeypatch.setattr(computers, "_checked_status", checked_status)
    task = asyncio.create_task(computers._maintain(websocket, session, stream_closed))

    async def advance(value: float) -> None:
        nonlocal now
        now = value
        # Drain ready callbacks and timeout cancellation without wall-clock sleeps.
        for _ in range(10):
            await asyncio.sleep(0)

    try:
        await advance(0)
        await advance(25)
        assert starts == [0, 25]
        await advance(29)
        assert completed == [0, 29]
        assert not task.done()
        await advance(50)
        assert starts == [0, 25, 50]
        await advance(55)
        assert task.done(), "Overdue authorization/worker check must close within 30 seconds"
        await task
        assert cancelled == [slow_phase]
        assert session.closed.is_set()
        assert stream_closed.is_set()
        with pytest.raises(ComputerError, match="Invalid or expired"):
            store.authenticate(session.session_id, session.session_token)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
