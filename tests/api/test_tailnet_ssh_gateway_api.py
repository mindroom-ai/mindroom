"""Tests for the gateway-only listener of tailnet SSH, and for SSH sessions keeping their workers up."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import asynccontextmanager, nullcontext
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
    tailnet_ssh_status,
)
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.runtime_env_policy import isolated_worker_runtime_env, public_worker_startup_env
from mindroom.tailnet_ssh.links import ConnectRefused, GatewayStatus, LinkStatus, resolve_target
from mindroom.tailnet_ssh.store import Link, new_link, update_links
from mindroom.workers.backend import WorkerBackend
from mindroom.workers.models import WorkerMaintenanceResult
from tests.conftest import SETTLE_TIMEOUT_SECONDS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

ALICE = "@alice:example.org"
AUTH = {"Authorization": "Bearer tok"}
PEER = {"peer_login": "alice@github", "peer_node": "laptop"}
# One well-formed call of each route.
CALLS: list[tuple[str, str, dict[str, object] | None]] = [
    ("GET", "/links", None),
    ("POST", "/status", {"enrollment": "login", "links": {}}),
    ("POST", "/links/abc/connect", PEER),
]
ROUTES = [(method, path) for method, path, _body in CALLS]
_DATA: dict[str, Any] = {
    "models": {"default": {"provider": "openai", "id": "m"}},
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


def _stored_link(api_app: FastAPI) -> Link:
    """Store Alice's link to her agent, enrolled, for the worker her turns run in."""
    config, paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    now = time.time()
    link = new_link(ALICE, "mine", resolve_target(config, paths, ALICE, "mine").worker_key, "mine", now=now)
    link = link.model_copy(update={"enrolled_login": "alice@github", "enrolled_at": now})
    update_links(paths, lambda book: book.links.append(link))
    return link


def _report(**fields: object) -> dict[str, object]:
    report: dict[str, object] = {
        "state": "running",
        "login_url": None,
        "tailnet": "tail1234.ts.net",
        "dns_name": "mine.tail1234.ts.net",
        "login_name": "alice@github",
        "key_expiry": None,
        "open_connections": 0,
        "last_error": None,
    }
    return report | fields


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
    on_event_loop = calls_on_event_loop(tailnet_ssh_gateway, "desired_links")

    response = client.get("/links", headers=AUTH)

    assert response.status_code == 200
    assert on_event_loop == [False]
    assert response.json() == [
        {
            "id": link.id,
            "hostname": "mine",
            "desired": "on",
            "allowed_login": "alice@github",
            "closed_before": link.closed_before,
            "login_after": link.login_after,
        },
    ]


def test_status_is_stored_and_answers_204(
    api_app: FastAPI,
    client: TestClient,
    calls_on_event_loop: Callable[[object, str], list[bool]],
) -> None:
    """The gateway's report is kept on the primary's app, each link's worker beside it, stored off the event loop."""
    link = _stored_link(api_app)
    on_event_loop = calls_on_event_loop(tailnet_ssh_gateway, "record_status")

    response = client.post(
        "/status",
        headers=AUTH,
        json={"enrollment": "login", "links": {link.id: _report(open_connections=2)}},
    )

    assert (response.status_code, response.content) == (204, b"")
    assert on_event_loop == [False]
    status = tailnet_ssh_status(api_app)
    assert status is not None
    assert status.enrollment == "login"
    assert status.links[link.id].state == "running"
    assert status.links[link.id].open_connections == 2
    assert active_tailnet_ssh_worker_keys(api_app) == {link.worker_key}


@pytest.mark.parametrize(
    "body",
    [
        {"links": {}},
        {"enrollment": "oauth", "links": {}},
        {"enrollment": "login", "links": {"abc": {"state": "sleeping"}}},
        {"enrollment": "login", "links": {"abc": _report(open_connections="many")}},
    ],
)
def test_status_outside_the_contract_is_refused_and_changes_nothing(
    api_app: FastAPI,
    client: TestClient,
    body: dict[str, object],
) -> None:
    """A report the contract does not describe answers 422 and leaves the last report as it was."""
    response = client.post("/status", headers=AUTH, json=body)

    assert response.status_code == 422
    assert tailnet_ssh_status(api_app) == GatewayStatus()


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

    monkeypatch.setattr(tailnet_ssh_gateway, "connect", connect)

    admitted = client.post("/links/abc/connect", headers=AUTH, json=PEER)
    outcome = ConnectRefused(409, "x")
    refused = client.post("/links/abc/connect", headers=AUTH, json=PEER)

    assert (admitted.status_code, admitted.json()) == (200, {"host": "10.0.0.5", "port": 2222})
    assert (refused.status_code, refused.json()) == (409, {"detail": "x"})
    config, paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    state = config_lifecycle.app_state(api_app)
    expected = (config, paths, state.runtime.agent_reply_memberships, state.tailnet_ssh_status)
    assert calls == [(*expected, "abc", "alice@github", "laptop")] * 2


def test_connect_of_an_unknown_link_is_the_services_404(client: TestClient) -> None:
    """The link service itself runs behind the route: a link the store does not hold is refused, unpatched."""
    response = client.post("/links/abc/connect", headers=AUTH, json=PEER)

    assert (response.status_code, response.json()) == (404, {"detail": "No such link."})


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
    assert tailnet_ssh_status(api_app) == GatewayStatus()


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

    assert (response.status_code, response.json()) == (503, {"detail": "Tailnet SSH is off."})


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
        assert tailnet_ssh_status(api_app) is None
        assert active_tailnet_ssh_worker_keys(api_app) == frozenset()
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    for taken in ({PORT_ENV: str(api_port)}, {PORT_ENV: str(port), "MINDROOM_SCRIPT_GATEWAY_PORT": str(port)}):
        with pytest.raises(ValueError, match=PORT_ENV):
            async with listener(**{TOKEN_ENV: "tok"}, **taken):
                pass
        assert tailnet_ssh_status(api_app) is None

    async with listener(**{TOKEN_ENV: "tok", PORT_ENV: str(port)}):
        assert tailnet_ssh_status(api_app) == GatewayStatus()
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
            assert (await http.get("/links")).status_code == 401
            assert (await http.get("/links", headers=AUTH)).json() == []
    assert tailnet_ssh_status(api_app) is None
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


def test_gateway_token_never_reaches_a_worker_env() -> None:
    """Neither a worker's startup manifest nor its runtime env carries the gateway's token."""
    assert public_worker_startup_env({TOKEN_ENV: "t"}) == {}
    assert isolated_worker_runtime_env({TOKEN_ENV: "t"}) == {}


def test_only_a_fresh_report_with_open_connections_holds_a_worker(api_app: FastAPI) -> None:
    """A link without connections holds nothing, and a report older than 90 seconds says nothing."""
    status = tailnet_ssh_status(api_app)
    assert status is not None
    status.links = {"busy": _link_status("worker-busy", 1), "quiet": _link_status("worker-quiet", 0)}

    status.received_at = time.monotonic()
    assert active_tailnet_ssh_worker_keys(api_app) == {"worker-busy"}
    status.received_at = time.monotonic() - 91
    assert active_tailnet_ssh_worker_keys(api_app) == frozenset()


def _link_status(worker_key: str, open_connections: int) -> LinkStatus:
    return LinkStatus("running", None, None, None, None, None, open_connections, None, worker_key)


@pytest.mark.asyncio
async def test_both_maintenance_paths_touch_ssh_workers(api_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker with an open SSH connection is touched by the background cleanup and by the cleanup route."""
    status = tailnet_ssh_status(api_app)
    assert status is not None
    status.links = {"abc": _link_status("worker-ssh", 1)}
    status.received_at = time.monotonic()
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
    assert actions == ["touch:worker-ssh", "maintain"]

    actions.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://primary") as http:
        assert (await http.post("/api/workers/cleanup")).status_code == 200
    assert actions == ["touch:worker-ssh", "maintain"]
