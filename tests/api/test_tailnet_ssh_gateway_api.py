"""Tests for the gateway-only listener of tailnet SSH, and for SSH sessions keeping their workers up."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api import config_lifecycle, tailnet_ssh_gateway
from mindroom.api import main as api_main
from mindroom.api import workers as workers_api
from mindroom.api.app_state import ensure_app_state
from mindroom.api.tailnet_ssh_gateway import (
    DEFAULT_PORT,
    PORT_ENV,
    TOKEN_ENV,
    active_tailnet_ssh_worker_keys,
    gateway_app,
    serve_tailnet_ssh_gateway_listener,
)
from mindroom.config.main import Config
from mindroom.constants import (
    RuntimePaths,
    is_workspace_env_overlay_name_allowed,
    resolve_runtime_paths,
    shell_extra_env_values,
)
from mindroom.runtime_env_policy import (
    is_trusted_tool_runtime_env_file_name,
    is_trusted_tool_runtime_process_env_name,
    isolated_worker_runtime_env,
    public_worker_startup_env,
    sandbox_runner_startup_process_env,
    worker_extra_env,
)
from mindroom.tailnet_ssh import links as link_service
from mindroom.tailnet_ssh.links import ConnectRefused, GatewayStatus, StatusReport
from mindroom.tailnet_ssh.store import Link, new_link, read_links, update_links
from mindroom.worker_computer.sessions import ComputerSessionStore
from mindroom.workers.backend import WorkerBackend
from mindroom.workers.models import WorkerMaintenanceResult, WorkerSpec, WorkerSshEndpoint
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.test_computer_sessions import authorized_target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

ALICE = "@alice:example.org"
_ALICE_MINE_KEY = "v1:default:user_agent:~@alice:example.org:mine"
AUTH = {"Authorization": "Bearer tok"}
PEER = {"peer_login": "alice@github", "peer_node": "laptop"}
# One well-formed call of each route.
CALLS: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/links", None),
    ("POST", "/status", {"open_connections": {}, "removed": []}),
    ("POST", "/links/abc/connect", PEER),
]
ROUTES = [(method, path) for method, path, _body in CALLS]
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPVHVwHKbi4VF0jfKNiYpfwAeEgeTqB6B8NVRZvzjnDS alice@laptop"
BAD_BODY = "Invalid gateway request body."
# What the Go gateway's own tests parse and send; the two sides are checked against each other nowhere else.
GATEWAY_FIXTURES = Path(__file__).parents[2] / "tailnet-gateway" / "testdata"
# Bodies neither POST route takes: not JSON, not an object, and an object without the route's fields.
BAD_BODIES = [
    (path, body)
    for path in ("/status", "/links/abc/connect")
    for body in (b"", b"not json", b"[]", b"{}", b'{"peer_login": 7, "peer_node": "laptop"}')
]
_DATA: dict[str, Any] = {
    "models": {"default": {"provider": "openai", "id": "m"}},
    "tailnet_ssh": {"logins": {ALICE: "alice@github"}, "authorized_keys": {ALICE: [PUBLIC_KEY]}},
    "agents": {
        "mine": {
            "display_name": "Mine",
            "worker_scope": "user_agent",
            "access": {"users": [ALICE], "members_of_rooms": []},
            "tailnet_ssh": True,
        },
    },
}


def _paths(tmp_path: Path, **env: str) -> RuntimePaths:
    """A primary with the Docker worker backend, and ``env`` beside it."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
            "MINDROOM_WORKER_BACKEND": "docker",
            **env,
        },
    )


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    """The primary's runtime."""
    return _paths(tmp_path)


@pytest.fixture
def api_app(paths: RuntimePaths) -> FastAPI:
    """The primary's API app with a published config and tailnet SSH on, serving the worker routes."""
    app = FastAPI()
    app.include_router(workers_api.router)
    state = ensure_app_state(app)
    config = Config.validate_with_runtime(deepcopy(_DATA), paths)
    state.api_state = config_lifecycle.ApiState(
        threading.Lock(),
        config_lifecycle.ApiSnapshot(1, paths, runtime_config=config),
    )
    state.embedded = SimpleNamespace(  # type: ignore[assignment]
        agent_reply_memberships=AgentReplyMembershipIndex(),
        script_worker_keepalive=lambda _manager: None,
    )
    state.tailnet_ssh_status = GatewayStatus()
    return app


@pytest.fixture
def client(api_app: FastAPI) -> TestClient:
    """The gateway's view of the primary."""
    return TestClient(gateway_app(api_app, "tok"))


def _stored_link(api_app: FastAPI, agent: str = "mine") -> Link:
    """Store Alice's link to the agent."""
    _, paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    link = new_link(ALICE, agent)
    update_links(paths, lambda book: book.links.append(link))
    return link


def _status(api_app: FastAPI) -> GatewayStatus | None:
    return config_lifecycle.app_state(api_app).tailnet_ssh_status


def _unload_config(api_app: FastAPI) -> None:
    state = config_lifecycle.require_api_state(api_app)
    state.snapshot = config_lifecycle.ApiSnapshot(2, state.snapshot.runtime_paths, load_error=ValueError("broken"))


@pytest.mark.parametrize("route", ROUTES)
def test_routes_need_the_bearer_token(client: TestClient, route: tuple[str, str]) -> None:
    """No token, a wrong one, one that is not ASCII, and the token without its scheme get the same 401, whatever the body."""
    method, path = route
    presented: list[dict[str, str | bytes]] = [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": "Bearer tök".encode()},
        {"Authorization": "tok"},
    ]
    answers = [client.request(method, path, headers=headers, content=b"not json") for headers in presented]

    assert [(answer.status_code, answer.json()) for answer in answers] == [(401, {"detail": "Unauthorized"})] * 4


def test_a_request_without_the_token_learns_nothing_of_the_primary(api_app: FastAPI, client: TestClient) -> None:
    """Without the token an unknown path, and a primary whose config is not loaded, answer as every route does."""
    unknown = client.get("/api/health")
    _unload_config(api_app)
    unloaded = client.get("/links")

    assert (unknown.status_code, unloaded.status_code) == (401, 401)
    assert unknown.content == unloaded.content
    assert client.get("/api/health", headers=AUTH).status_code == 404


def test_listener_app_has_only_three_routes(api_app: FastAPI) -> None:
    """The listener serves the gateway's three routes: no docs, no schema, nothing of the primary API."""
    app = gateway_app(api_app, "tok")

    assert {route.path for route in app.routes} == {"/links", "/status", "/links/{link_id}/connect"}  # type: ignore[attr-defined]


def test_get_links_returns_desired_rows(
    api_app: FastAPI,
    client: TestClient,
    calls_on_event_loop: Callable[[object, str], list[bool]],
) -> None:
    """A stored link that is on is one row of what the gateway is to run, read from the store off the event loop."""
    link = _stored_link(api_app)
    on_event_loop = calls_on_event_loop(link_service, "_list_workers")

    response = client.get("/links", headers=AUTH)

    assert response.status_code == 200
    assert on_event_loop == [False]
    assert len(response.json()[0]["access_revision"]) == 64
    assert response.json() == [
        {
            "id": link.id,
            "hostname": "mine",
            "desired": "on",
            "allowed_login": "alice@github",
            "access_revision": response.json()[0]["access_revision"],
        },
    ]


def test_get_links_clears_the_keys_of_a_removed_links_worker(
    api_app: FastAPI,
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    calls_on_event_loop: Callable[[object, str], list[bool]],
) -> None:
    """A link the reconcile removes is answered as ``removing``, its worker's keys emptied off the event loop first.

    Its sshd's stop is left to run apart from the answer.
    """
    config, _ = config_lifecycle.read_app_committed_runtime_config(api_app)
    moved = _stored_link(api_app)
    del config.agents["mine"]
    ssh_dir = tmp_path / "ssh"
    link_service.write_projection(ssh_dir, moved, [f"restrict,pty {PUBLIC_KEY}"])
    handle = SimpleNamespace(
        worker_key=_ALICE_MINE_KEY,
        ssh_endpoint=SimpleNamespace(projection_dir=ssh_dir),
    )
    handle.status = "ready"
    manager = MagicMock(list_workers=MagicMock(return_value=[handle]))
    manager.__enter__.return_value = manager
    monkeypatch.setattr(link_service, "lease_configured_primary_worker_manager", lambda _paths: manager)

    async def stop(*_args: object) -> None:
        pass

    monkeypatch.setattr(link_service, "_stop_sshd", stop)
    on_event_loop = calls_on_event_loop(link_service, "clear_authorized_keys")

    response = client.get("/links", headers=AUTH)

    assert [(row["id"], row["desired"]) for row in response.json()] == [(moved.id, "removing")]
    assert (ssh_dir / "authorized_keys").read_text() == ""
    assert on_event_loop == [False]


@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("index", ["fresh", "invalidated"])
def test_get_links_omits_a_room_granted_link_while_membership_is_pending(
    api_app: FastAPI,
    client: TestClient,
    index: str,
) -> None:
    """At a primary's start, or after a reload, room membership is not known: a room-granted link is omitted, not retired."""
    data = deepcopy(_DATA)
    data["agents"]["research"] = {
        "display_name": "Research",
        "private": {"per": "user_agent"},
        "tailnet_ssh": True,
        "rooms": ["lobby"],
    }
    state = config_lifecycle.require_api_state(api_app)
    paths = state.snapshot.runtime_paths
    config = Config.validate_with_runtime(data, paths)
    state.snapshot = config_lifecycle.ApiSnapshot(2, paths, runtime_config=config)
    if index == "invalidated":
        config_lifecycle.app_state(api_app).runtime.agent_reply_memberships.invalidate(config, reason="config_reload")
    link = _stored_link(api_app, "research")

    response = client.get("/links", headers=AUTH)

    assert response.status_code == 200
    assert response.json() == []
    assert read_links(paths).by_id(link.id) == link


def test_status_is_stored_and_answers_204(
    api_app: FastAPI,
    client: TestClient,
    calls_on_event_loop: Callable[[object, str], list[bool]],
) -> None:
    """The gateway's report is kept on the primary's app, each link's worker beside it, stored off the event loop."""
    link = _stored_link(api_app)
    on_event_loop = calls_on_event_loop(link_service, "record_status")

    response = client.post(
        "/status",
        headers=AUTH,
        json={"open_connections": {link.id: 2}, "removed": []},
    )

    assert (response.status_code, response.content) == (204, b"")
    assert on_event_loop == [False]
    assert active_tailnet_ssh_worker_keys(api_app) == {_ALICE_MINE_KEY}


@pytest.mark.parametrize(
    "body",
    [
        {"open_connections": {}},
        {"removed": [], "open_connections": {"abc": -1}},
        {"removed": [], "open_connections": {"abc": "2"}},
        {"removed": [], "open_connections": {"abc": True}},
        {"removed": [1], "open_connections": {}},
        {"removed": [], "open_connections": {}, "unexpected": "secret-value"},
    ],
)
def test_status_outside_the_contract_is_refused_and_changes_nothing(
    api_app: FastAPI,
    client: TestClient,
    body: dict[str, object],
) -> None:
    """Invalid status never publishes activity, retires IDs, or echoes values."""
    response = client.post("/status", headers=AUTH, json=body)
    assert (response.status_code, response.json()) == (422, {"detail": BAD_BODY})
    assert _status(api_app) == GatewayStatus()


def test_a_refused_body_does_not_log_request_keys_or_values(client: TestClient) -> None:
    """Validation locations and rejected values may be sensitive."""
    body = {"open_connections": {"sensitive-field": "sensitive-value"}, "removed": []}
    with capture_logs() as logs:
        response = client.post("/status", headers=AUTH, json=body)
    assert response.status_code == 422
    assert "sensitive" not in repr(logs)
    assert "sensitive" not in response.text


@pytest.mark.parametrize(("path", "body"), BAD_BODIES)
def test_a_body_that_is_not_the_contracts_answers_422_without_repeating_it(
    client: TestClient,
    path: str,
    body: bytes,
) -> None:
    """Whatever a POST carries that is not its route's JSON document, the answer is one sentence."""
    response = client.post(path, headers=AUTH, content=body)

    assert (response.status_code, response.json()) == (422, {"detail": BAD_BODY})


def test_connect_answers_endpoint_or_refusal_detail(
    api_app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect answers where the worker's sshd listens, or the service's refusal with its status and detail."""
    calls: list[tuple[object, ...]] = []
    outcome: tuple[str, int] | ConnectRefused = ("10.0.0.5", 2222)

    async def connect(*args: object) -> tuple[str, int]:
        calls.append(args)
        if isinstance(outcome, ConnectRefused):
            raise outcome
        return outcome

    monkeypatch.setattr(link_service, "connect", connect)

    admitted = client.post("/links/abc/connect", headers=AUTH, json=PEER)
    outcome = ConnectRefused(409, "x")
    refused = client.post("/links/abc/connect", headers=AUTH, json=PEER)

    assert (admitted.status_code, admitted.json()) == (200, {"host": "10.0.0.5", "port": 2222})
    assert (refused.status_code, refused.json()) == (409, {"detail": "x"})
    config, paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    state = config_lifecycle.app_state(api_app)
    for read, *parameters in calls:
        assert callable(read)
        assert read() == (config, paths)
        assert parameters == [state.runtime.agent_reply_memberships, "abc", "alice@github", "laptop"]


def test_connect_of_an_unknown_link_is_the_services_404(client: TestClient) -> None:
    """The link service itself runs behind the route: a link the store does not hold is refused, unpatched."""
    response = client.post("/links/abc/connect", headers=AUTH, json=PEER)

    assert (response.status_code, response.json()) == (404, {"detail": "No such link."})


def test_the_go_gateways_fixtures_are_what_this_listener_takes_and_answers(
    api_app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both row variants and both POST bodies match the gateway's shared fixtures."""
    status_body = (GATEWAY_FIXTURES / "status.json").read_bytes()
    connect_body = (GATEWAY_FIXTURES / "connect.json").read_bytes()
    on, removing = expected = json.loads((GATEWAY_FIXTURES / "links.json").read_text())
    link_id, removed_id = on["id"], removing["id"]
    config, paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    config.tailnet_ssh.logins[ALICE] = "bas@github"
    link = new_link(ALICE, "mine").model_copy(update={"id": link_id, "hostname": "mind-bas"})
    deleted = new_link(ALICE, "code").model_copy(update={"id": removed_id})
    update_links(paths, lambda book: book.links.extend([link, deleted]))
    peers: list[tuple[object, ...]] = []

    async def connect(*args: object) -> tuple[str, int]:
        peers.append(args[-3:])
        return "10.0.0.5", 2222

    monkeypatch.setattr(link_service, "connect", connect)
    assert client.get("/links", headers=AUTH).json() == expected
    posted = client.post("/status", headers=AUTH, content=status_body)
    assert (posted.status_code, posted.content) == (204, b"")
    status = StatusReport.model_validate_json(status_body)
    assert status.open_connections == {link_id: 2}
    assert read_links(paths).links == [link]
    assert active_tailnet_ssh_worker_keys(api_app) == {_ALICE_MINE_KEY}
    connected = client.post(f"/links/{link_id}/connect", headers=AUTH, content=connect_body)
    assert (connected.status_code, connected.json()) == (200, {"host": "10.0.0.5", "port": 2222})
    assert peers == [(link_id, "bas@github", "laptop")]


@pytest.mark.parametrize("revoked", ["login", "keys", "opt-in"])
def test_connect_rereads_the_committed_config_replaced_during_wake(
    api_app: FastAPI,
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    revoked: str,
) -> None:
    """A real committed snapshot change during ensure_worker must prevent projection and /ssh/start."""
    link = _stored_link(api_app)
    original, paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    changed = original.model_copy(deep=True)
    if revoked == "login":
        changed.tailnet_ssh.logins[ALICE] = "new@login"
    elif revoked == "keys":
        changed.tailnet_ssh.authorized_keys.clear()
    else:
        changed.agents["mine"].tailnet_ssh = False
    state = config_lifecycle.require_api_state(api_app)
    ssh = tmp_path / "ssh"
    manager = MagicMock()
    manager.__enter__.return_value = manager

    def wake(spec: WorkerSpec) -> SimpleNamespace:
        assert spec.worker_key == _ALICE_MINE_KEY
        with state.config_lock:
            state.snapshot = config_lifecycle.ApiSnapshot(2, paths, runtime_config=changed)
        return SimpleNamespace(
            worker_key=spec.worker_key,
            status="ready",
            ssh_endpoint=WorkerSshEndpoint("worker", 2222, ssh),
        )

    manager.ensure_worker.side_effect = wake
    monkeypatch.setattr(link_service, "lease_configured_primary_worker_manager", lambda _paths: manager)
    runner = MagicMock()
    monkeypatch.setattr(link_service, "_start_sshd", runner)
    reply = client.post(f"/links/{link.id}/connect", headers=AUTH, json=PEER)
    manager.ensure_worker.assert_called_once()
    assert reply.status_code == (403 if revoked == "login" else 409)
    assert not ssh.exists()
    runner.assert_not_called()


@pytest.mark.parametrize(("method", "path", "body"), CALLS)
def test_config_not_loaded_answers_503(
    api_app: FastAPI,
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    """While the primary's config does not load, the gateway is told to try again, not why the config fails."""
    _unload_config(api_app)

    response = client.request(method, path, headers=AUTH, json=body)

    assert (response.status_code, response.json()) == (503, {"detail": "MindRoom's configuration is not loaded."})
    assert _status(api_app) == GatewayStatus()


@pytest.mark.parametrize(("path", "body"), BAD_BODIES)
def test_an_unready_primary_answers_503_before_it_reads_a_body(
    api_app: FastAPI,
    client: TestClient,
    path: str,
    body: bytes,
) -> None:
    """A primary that cannot serve the gateway says so whatever the request carries, as it does for a good one."""
    _unload_config(api_app)
    unloaded = client.post(path, headers=AUTH, content=body)
    config_lifecycle.app_state(api_app).tailnet_ssh_status = None
    off = client.post(path, headers=AUTH, content=body)

    assert (unloaded.status_code, unloaded.json()) == (503, {"detail": "MindRoom's configuration is not loaded."})
    assert (off.status_code, off.json()) == (503, {"detail": "Tailnet SSH is disabled."})


@pytest.mark.parametrize(("method", "path", "body"), CALLS)
def test_routes_answer_503_once_tailnet_ssh_is_off(
    api_app: FastAPI,
    client: TestClient,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    """An app whose listener has stopped keeps no report, so its routes serve nothing."""
    config_lifecycle.app_state(api_app).tailnet_ssh_status = None

    response = client.request(method, path, headers=AUTH, json=body)

    assert (response.status_code, response.json()) == (503, {"detail": "Tailnet SSH is disabled."})


@pytest.mark.asyncio
async def test_listener_is_off_without_token_and_refuses_the_api_port(
    api_app: FastAPI,
    tmp_path: Path,
    unused_tcp_port_factory: Callable[[], int],
) -> None:
    """The listener serves only while the primary has the gateway's token, and never on another listener's port."""
    config_lifecycle.app_state(api_app).tailnet_ssh_status = None
    port, api_port = unused_tcp_port_factory(), unused_tcp_port_factory()

    def listener(**env: str) -> Any:  # noqa: ANN401
        return serve_tailnet_ssh_gateway_listener(
            _paths(tmp_path, **env),
            host="127.0.0.1",
            api_port=api_port,
            api_app=api_app,
            log_level="INFO",
        )

    async with listener(**{PORT_ENV: str(port)}):
        assert _status(api_app) is None
        assert active_tailnet_ssh_worker_keys(api_app) == frozenset()
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    for taken in ({PORT_ENV: str(api_port)}, {PORT_ENV: str(port), "MINDROOM_SCRIPT_GATEWAY_PORT": str(port)}):
        with pytest.raises(ValueError, match=PORT_ENV):
            async with listener(**{TOKEN_ENV: "tok"}, **taken):
                pass
        assert _status(api_app) is None

    async with listener(**{TOKEN_ENV: "tok", PORT_ENV: str(port)}):
        assert _status(api_app) == GatewayStatus()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            assert (await http.get("/links")).status_code == 401
            assert (await http.get("/links", headers=AUTH)).json() == []
    assert _status(api_app) is None
    socket.create_server(("127.0.0.1", port)).close()


@pytest.mark.asyncio
async def test_listener_port_defaults_to_8769(
    api_app: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``MINDROOM_TAILNET_SSH_PORT`` the listener takes 8769, on the host it is given."""
    served: list[dict[str, object]] = []

    @asynccontextmanager
    async def serve_listener(_app: object, **options: object) -> AsyncIterator[None]:
        served.append(options)
        yield

    monkeypatch.setattr(tailnet_ssh_gateway, "serve_listener", serve_listener)

    async with serve_tailnet_ssh_gateway_listener(
        _paths(tmp_path, **{TOKEN_ENV: "tok"}),
        host="10.1.2.3",
        api_port=8765,
        api_app=api_app,
        log_level="INFO",
    ):
        pass

    assert DEFAULT_PORT == 8769
    assert served == [
        {
            "name": "tailnet_ssh_gateway_listener",
            "host": "10.1.2.3",
            "port": 8769,
            "log_level": "INFO",
            "ws": "none",
            "access_log": False,
        },
    ]


@pytest.mark.parametrize("pattern", ["MINDROOM_*", "*_TOKEN", "*", TOKEN_ENV])
def test_gateway_token_never_reaches_a_worker_env(pattern: str) -> None:
    """No worker's startup manifest, runtime env, or extra env carries the gateway's token, nor does a tool's env.

    A shell passthrough pattern an operator sets gives an agent's shell the other values it names, never the token.
    """
    assert public_worker_startup_env({TOKEN_ENV: "t"}) == {}
    assert isolated_worker_runtime_env({TOKEN_ENV: "t"}) == {}
    assert worker_extra_env({TOKEN_ENV: "t"}) == {}
    assert sandbox_runner_startup_process_env({TOKEN_ENV: "t"}) == {}
    assert not is_trusted_tool_runtime_env_file_name(TOKEN_ENV)
    assert not is_trusted_tool_runtime_process_env_name(TOKEN_ENV)
    assert not is_workspace_env_overlay_name_allowed(TOKEN_ENV)

    env = {TOKEN_ENV: "t", "MINDROOM_SERVICE_TOKEN": "passed"}
    passed = shell_extra_env_values(extra_env_passthrough=f"MINDROOM_SERVICE_TOKEN,{pattern}", process_env=env)

    assert dict(passed) == {"MINDROOM_SERVICE_TOKEN": "passed"}


def test_only_a_fresh_report_with_open_connections_holds_a_worker(api_app: FastAPI) -> None:
    """A link without connections holds nothing, and a report older than 90 seconds says nothing."""
    status = _status(api_app)
    assert status is not None
    keys = frozenset({"worker-busy"})

    status.report = (time.monotonic(), keys)
    assert active_tailnet_ssh_worker_keys(api_app) == {"worker-busy"}
    status.report = (time.monotonic() - 91, keys)
    assert active_tailnet_ssh_worker_keys(api_app) == frozenset()


@pytest.mark.asyncio
async def test_both_maintenance_paths_touch_ssh_workers(api_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both the background cleanup and the cleanup route touch a worker with an open SSH connection.

    They touch the worker a Computer viewer holds as before.
    """
    status = _status(api_app)
    assert status is not None
    status.report = (time.monotonic(), frozenset({"worker-ssh"}))
    computer_sessions = ComputerSessionStore()
    # A viewer whose stream is open holds its worker, which `authorized_target` names "worker".
    computer_sessions.create(authorized_target()).stream = asyncio.Event()
    config_lifecycle.app_state(api_app).computer_sessions = computer_sessions
    held = ["touch:worker", "touch:worker-ssh"]
    actions: list[str] = []
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    manager = MagicMock(spec=WorkerBackend)
    manager.idle_timeout_seconds = 60.0
    manager.touch_worker.side_effect = lambda worker_key: actions.append("touch:" + worker_key)

    def maintain(_manager: WorkerBackend) -> WorkerMaintenanceResult:
        actions.append("maintain")
        # The background cleanup stops after its first pass.
        loop.call_soon_threadsafe(stop_event.set)
        return WorkerMaintenanceResult((), ())

    monkeypatch.setattr(workers_api, "maintain_workers", maintain)
    for module in (workers_api, api_main):
        monkeypatch.setattr(module, "lease_configured_primary_worker_manager", lambda _paths: nullcontext(manager))
    monkeypatch.setattr(api_main, "worker_cleanup_interval_seconds", lambda _paths: 0.01)

    await asyncio.wait_for(
        api_main._worker_cleanup_loop(stop_event, api_app, touch_live_workers=lambda _manager: None),
        timeout=SETTLE_TIMEOUT_SECONDS,
    )
    assert (sorted(actions[:-1]), actions[-1]) == (held, "maintain")

    actions.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://primary") as http:
        assert (await http.post("/api/workers/cleanup")).status_code == 200
    assert (sorted(actions[:-1]), actions[-1]) == (held, "maintain")
