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
    tailnet_ssh_status,
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
from mindroom.tailnet_ssh.links import (
    ConnectRefused,
    GatewayStatus,
    LinkReport,
    LinkStatus,
    _GatewayReport,
    _resolve_target,
)
from mindroom.tailnet_ssh.store import Link, new_link, read_links, update_links
from mindroom.worker_computer.sessions import ComputerSessionStore
from mindroom.workers.backend import WorkerBackend
from mindroom.workers.models import WorkerMaintenanceResult
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.test_computer_sessions import authorized_target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

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
LOGIN_URL = "https://login.tailscale.com/a/1c0ffee"
BAD_BODY = "The request body is not what the gateway contract describes."
# What the Go gateway's own tests parse and send; the two sides are checked against each other nowhere else.
GATEWAY_FIXTURES = Path(__file__).parents[2] / "tailnet-gateway" / "testdata"
# Bodies neither POST route takes: not JSON, not an object, and an object without the route's fields.
BAD_BODIES = [
    (path, body)
    for path in ("/status", "/links/abc/connect")
    for body in (b"", b"not json", b"[]", b"{}", b'{"peer_login": 7, "peer_node": "laptop", "enrollment": "oauth"}')
]
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


def _stored_link(api_app: FastAPI, agent: str = "mine") -> Link:
    """Store Alice's link to the agent, enrolled, for the worker her turns run in."""
    config, paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    now = time.time()
    link = new_link(ALICE, agent, _resolve_target(config, paths, ALICE, agent).worker_key, agent, now=now)
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
    _config, paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    moved = new_link(ALICE, "mine", "v1:elsewhere:user_agent:~@alice:example.org:mine", "mine", now=time.time())
    update_links(paths, lambda book: book.links.append(moved))
    ssh_dir = tmp_path / "ssh"
    link_service.write_projection(ssh_dir, moved, ["restrict,pty ssh-ed25519 AAAA alice@laptop"])
    handle = SimpleNamespace(worker_key=moved.worker_key, ssh_endpoint=SimpleNamespace(projection_dir=ssh_dir))
    manager = MagicMock(list_workers=MagicMock(return_value=[handle]))
    manager.__enter__.return_value = manager
    monkeypatch.setattr(link_service, "lease_configured_primary_worker_manager", lambda _paths: manager)
    stops: list[object] = []
    monkeypatch.setattr(link_service, "_stop_in_background", lambda _paths, handles: stops.extend(handles))
    on_event_loop = calls_on_event_loop(link_service, "_clear_revoked_workers")

    response = client.get("/links", headers=AUTH)

    assert [(row["id"], row["desired"]) for row in response.json()] == [(moved.id, "removing")]
    assert (ssh_dir / "authorized_keys").read_text() == ""
    assert on_event_loop == [False]
    assert stops == [handle]


@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("index", ["fresh", "invalidated"])
def test_get_links_suspends_a_room_granted_link_while_membership_is_pending(
    api_app: FastAPI,
    client: TestClient,
    index: str,
) -> None:
    """At a primary's start, or after a reload, room membership is not known: a room-granted link waits, suspended."""
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
    assert [(row["id"], row["desired"], row["closed_before"]) for row in response.json()] == [
        (link.id, "suspended", link.closed_before),
    ]
    assert read_links(paths).by_id(link.id) == link


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
    assert read_links(config_lifecycle.read_app_committed_runtime_config(api_app)[1]).enrollment == "login"
    assert status.report.links[link.id].state == "running"
    assert status.report.links[link.id].open_connections == 2
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
    body: dict[str, Any],
) -> None:
    """A report the contract does not describe answers 422, repeats nothing it carried, and changes nothing.

    The report holds login URLs, which leave the primary only for a link's owner.
    """
    body = {**body, "links": {**body["links"], "def": _report(login_url=LOGIN_URL)}}

    response = client.post("/status", headers=AUTH, json=body)

    assert (response.status_code, response.json()) == (422, {"detail": BAD_BODY})
    assert LOGIN_URL not in response.text
    assert tailnet_ssh_status(api_app) == GatewayStatus()


def test_a_refused_body_logs_how_many_errors_and_their_kinds_but_never_where_or_what(client: TestClient) -> None:
    """The operator reads how the body broke the contract, without any value or key the gateway sent.

    An error's location holds the report's own keys, such as link ids, so a malformed report could put a login URL
    there; the log names no location. At most ten kinds are listed.
    """
    links = {LOGIN_URL: _report(state="sleeping")} | {f"link{index}": _report(state="sleeping") for index in range(10)}
    body = {"enrollment": "oauth", "links": links}

    with capture_logs() as logs:
        response = client.post("/status", headers=AUTH, json=body)

    assert response.status_code == 422
    refused = [log for log in logs if log["event"] == "tailnet_ssh_gateway_body_refused"]
    assert [
        (log["log_level"], log["model"], log["error_count"], log["error_types"], "errors" in log) for log in refused
    ] == [("warning", "StatusReport", 12, ["literal_error"] * 10, False)]
    assert "oauth" not in repr(logs)
    assert LOGIN_URL not in repr(logs)
    assert "link0" not in repr(logs)


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

    monkeypatch.setattr(tailnet_ssh_gateway, "connect", connect)

    admitted = client.post("/links/abc/connect", headers=AUTH, json=PEER)
    outcome = ConnectRefused(409, "x")
    refused = client.post("/links/abc/connect", headers=AUTH, json=PEER)

    assert (admitted.status_code, admitted.json()) == (200, {"host": "10.0.0.5", "port": 2222})
    assert (refused.status_code, refused.json()) == (409, {"detail": "x"})
    config, paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    state = config_lifecycle.app_state(api_app)
    expected = (config, paths, state.runtime.agent_reply_memberships)
    assert calls == [(*expected, "abc", "alice@github", "laptop")] * 2


def test_connect_of_an_unknown_link_is_the_services_404(client: TestClient) -> None:
    """The link service itself runs behind the route: a link the store does not hold is refused, unpatched."""
    response = client.post("/links/abc/connect", headers=AUTH, json=PEER)

    assert (response.status_code, response.json()) == (404, {"detail": "No such link."})


def test_the_go_gateways_fixtures_are_what_this_listener_takes_and_answers(
    api_app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bodies the Go gateway sends are read field by field, and ``GET /links`` answers rows it can parse."""
    status_body = (GATEWAY_FIXTURES / "status.json").read_bytes()
    connect_body = (GATEWAY_FIXTURES / "connect.json").read_bytes()
    gateway_rows = json.loads((GATEWAY_FIXTURES / "links.json").read_bytes())
    sent_peer = json.loads(connect_body)
    ((link_id, sent_report),) = json.loads(status_body)["links"].items()
    link = _stored_link(api_app)
    _config, paths = config_lifecycle.read_app_committed_runtime_config(api_app)
    update_links(paths, lambda book: setattr(book.by_id(link.id), "id", link_id))
    peers: list[tuple[object, ...]] = []

    async def connect(*args: object) -> tuple[str, int]:
        peers.append(args[-3:])
        return ("10.0.0.5", 2222)

    monkeypatch.setattr(tailnet_ssh_gateway, "connect", connect)

    rows = client.get("/links", headers=AUTH).json()
    posted = client.post("/status", headers=AUTH, content=status_body)
    connected = client.post(f"/links/{link_id}/connect", headers=AUTH, content=connect_body)

    # A field the listener does not know would be dropped without a word, so each one is read back.
    assert (posted.status_code, posted.content) == (204, b"")
    status = tailnet_ssh_status(api_app)
    assert status is not None
    assert status.report.enrollment == json.loads(status_body)["enrollment"] == "auth_key"
    assert sent_report.keys() == LinkReport.model_fields.keys()
    assert None not in sent_report.values()
    report = status.report.links[link_id]
    assert {field: getattr(report, field) for field in sent_report} == sent_report
    assert (connected.status_code, connected.json()) == (200, {"host": "10.0.0.5", "port": 2222})
    assert peers == [(link_id, sent_peer["peer_login"], sent_peer["peer_node"])]
    # The rows have the keys and value types of the ones the Go client's test parses.
    assert [row["id"] for row in rows] == [link_id]
    assert {row["desired"] for row in gateway_rows} == {"on", "removing"}
    for row in rows:
        assert all(row.keys() == gateway_row.keys() for gateway_row in gateway_rows)
        for key, value in row.items():
            assert type(value) in {type(gateway_row[key]) for gateway_row in gateway_rows}, key
    assert {type(gateway_row["allowed_login"]) for gateway_row in gateway_rows} == {str, type(None)}


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
    assert (off.status_code, off.json()) == (503, {"detail": "Tailnet SSH is off."})


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
    status = tailnet_ssh_status(api_app)
    assert status is not None
    links = {"busy": _link_status("worker-busy", 1), "quiet": _link_status("worker-quiet", 0)}

    status.report = _GatewayReport(time.monotonic(), links, "login")
    assert active_tailnet_ssh_worker_keys(api_app) == {"worker-busy"}
    status.report = _GatewayReport(time.monotonic() - 91, links, "login")
    assert active_tailnet_ssh_worker_keys(api_app) == frozenset()


def _link_status(worker_key: str, open_connections: int) -> LinkStatus:
    return LinkStatus("running", None, None, None, None, None, open_connections, None, worker_key)


@pytest.mark.asyncio
async def test_both_maintenance_paths_touch_ssh_workers(api_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both the background cleanup and the cleanup route touch a worker with an open SSH connection.

    They touch the worker a Computer viewer holds as before.
    """
    status = tailnet_ssh_status(api_app)
    assert status is not None
    status.report = _GatewayReport(time.monotonic(), {"abc": _link_status("worker-ssh", 1)}, "login")
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
