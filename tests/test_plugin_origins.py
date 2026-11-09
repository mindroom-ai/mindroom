"""Plugins serve pages on hosts of their own, ``<label>.<plugin>.<domain>``, from a listener beside the API."""

from __future__ import annotations

import json
import socket
import threading
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from mindroom.api import config_lifecycle
from mindroom.api.app_state import ensure_app_state
from mindroom.api.plugin_origins import _PluginOrigins, plugin_origin_url, serve_plugin_origins
from mindroom.api.plugin_routes import mount_plugin_routes
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.plugin_check import check_plugin
from mindroom.plugin_lifecycle import PluginLifespans
from mindroom.tool_system.plugin_imports import PluginValidationError
from mindroom.tool_system.plugins import config_plugins
from tests.conftest import bind_runtime_paths, publish_test_plugins

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:example.org"
ALICE = "@alice:example.org"
CHAT = "https://chat.example.org"
FLOOR = f"frame-ancestors 'self' {CHAT}"

ORIGIN = """
import asyncio

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse

from mindroom.plugin_http import ClientUser, client_origins, plugin_origin_url, stream_room_reader

router = APIRouter()
VERSION = 1


@router.get("/")
async def page() -> HTMLResponse:
    return HTMLResponse("<p>app</p>", headers={"content-security-policy": "default-src 'self'"})


@router.get("/whoami")
async def whoami(request: Request) -> dict:
    return {
        "app": id(request.app),
        "host": request.url.hostname,
        "sibling": plugin_origin_url(request, "sibling"),
        "chat": list(client_origins(request)),
    }


@router.get("/rooms/{room_id}/members/{user_id}")
async def member(request: Request, room_id: str, user_id: str) -> dict:
    room = await stream_room_reader(request, ClientUser(user_id), room_id)
    return {"user_id": room.user.user_id}


@router.get("/boom")
async def boom() -> dict:
    raise RuntimeError("plugin secret in a traceback")


@router.websocket("/live")
async def live(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text(f"v{VERSION}")
    await websocket.receive_text()


async def serves(label: str) -> object:
    if label == "slow":
        await asyncio.sleep(60)
    if label == "boom":
        raise RuntimeError("serves failed")
    return {"yes": True, "truthy": 1}.get(label, False)
"""

# A route a plugin serves on the API, which links to one of its hosts.
ROUTES = """
from fastapi import APIRouter, Request

from mindroom.plugin_http import plugin_origin_url

router = APIRouter()


@router.get("/link")
async def link(request: Request) -> dict:
    return {"url": plugin_origin_url(request, "item-1")}
"""


def _write(tmp_path: Path, name: str, **modules: str) -> Path:
    """Write plugin ``name`` with each ``<kind>_module`` given as its source; return its root."""
    root = tmp_path / "plugins" / name
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name}
    for kind, source in modules.items():
        manifest[f"{kind}_module"] = f"{kind}.py"
        (root / f"{kind}.py").write_text(source, encoding="utf-8")
    (root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _paths(tmp_path: Path, **env: str) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
            "MINDROOM_COMPUTER_ALLOWED_ORIGINS": json.dumps([CHAT]),
            "MINDROOM_PLUGIN_ORIGIN_PORT": "9100",
            **env,
        },
    )


def _api_app(tmp_path: Path, *names: str, **env: str) -> FastAPI:
    """Publish the plugins ``names`` and return an API app serving them, as ``mindroom run`` does."""
    runtime_paths = _paths(tmp_path, **env)
    config = bind_runtime_paths(Config(plugins=[f"./plugins/{name}" for name in names]), runtime_paths)
    publish_test_plugins(config, runtime_paths)
    app = FastAPI()
    ensure_app_state(app).api_state = config_lifecycle.ApiState(
        threading.Lock(),
        config_lifecycle.ApiSnapshot(1, runtime_paths, runtime_config=config),
    )
    mount_plugin_routes(app)
    return app


def _client(app: FastAPI, host: str) -> TestClient:
    return TestClient(_PluginOrigins(app), base_url=f"http://{host}", raise_server_exceptions=False)


def test_a_plugin_host_reaches_its_plugins_origin_router_with_the_api_app(tmp_path: Path) -> None:
    """A request on ``<label>.<plugin>.<domain>`` reaches that plugin's router, which reads the API app."""
    _write(tmp_path, "demo", origin=ORIGIN)
    app = _api_app(tmp_path, "demo")

    answer = _client(app, "x.demo.localhost:9100").get("/whoami")

    assert answer.status_code == 200
    assert answer.json() == {
        "app": id(app),
        "host": "x.demo.localhost",
        "sibling": "http://sibling.demo.localhost:9100",
        "chat": [CHAT],
    }
    assert answer.headers.get_list("content-security-policy") == [FLOOR]


def test_the_frame_floor_stands_beside_the_routes_own_policy(tmp_path: Path) -> None:
    """A route's own policy still applies, and only the chat and the host itself may frame what it answers."""
    _write(tmp_path, "demo", origin=ORIGIN)

    answer = _client(_api_app(tmp_path, "demo"), "x.demo.localhost").get("/")

    assert answer.text == "<p>app</p>"
    assert answer.headers.get_list("content-security-policy") == ["default-src 'self'", FLOOR]


def test_a_raising_origin_route_answers_500_without_its_detail(tmp_path: Path) -> None:
    """The error's detail never reaches the browser."""
    _write(tmp_path, "demo", origin=ORIGIN)

    answer = _client(_api_app(tmp_path, "demo"), "x.demo.localhost").get("/boom")

    assert answer.status_code == 500
    assert "secret" not in answer.text
    assert answer.headers.get_list("content-security-policy") == [FLOOR]


@pytest.mark.parametrize(
    "host",
    [
        "x.other.localhost",  # a plugin with routes but no origin module
        "x.missing.localhost",  # no such plugin
        "a.x.demo.localhost",  # deeper than one label
        "demo.localhost",  # no label
        "localhost",
        "x.demo.example.org",  # another domain
        "x_y.demo.localhost",  # not a DNS label
    ],
)
def test_hosts_that_are_no_published_plugins_origin_answer_404(tmp_path: Path, host: str) -> None:
    """Only a published plugin with an origin module serves its own host space, and nothing serves another's."""
    _write(tmp_path, "demo", origin=ORIGIN)
    _write(tmp_path, "other", routes=ROUTES)

    answer = _client(_api_app(tmp_path, "demo", "other"), host).get("/whoami")

    assert (answer.status_code, answer.json()) == (404, {"detail": "Not Found"})
    assert answer.headers.get_list("content-security-policy") == [FLOOR]


def test_an_origin_route_checks_membership_for_the_user_its_own_gate_names(tmp_path: Path) -> None:
    """``stream_room_reader`` takes a plain request and checks the user the route names, not any session."""
    _write(tmp_path, "demo", origin=ORIGIN)
    app = _api_app(tmp_path, "demo")
    client = _client(app, "x.demo.localhost")

    def member(user_id: str) -> tuple[int, object]:
        answer = client.get(f"/rooms/{ROOM}/members/{user_id}")
        return answer.status_code, answer.json()

    assert member(ALICE)[0] == 503

    async def room_members(room_id: str) -> frozenset[str]:
        return frozenset({ALICE}) if room_id == ROOM else frozenset()

    state = ensure_app_state(app)
    state.room_members = room_members
    # A room reader reads nothing else of the room until the route asks.
    state.room_state = state.room_direct = state.room_history = AsyncMock()

    assert member(ALICE) == (200, {"user_id": ALICE})
    assert member("@bob:example.org") == (403, {"detail": "Not a member of this room."})


@pytest.mark.parametrize(
    ("domain", "status"),
    [
        ("yes.demo.localhost", 200),
        ("YES.demo.localhost.", 200),
        ("no.demo.localhost", 404),
        ("truthy.demo.localhost", 404),
        ("boom.demo.localhost", 404),
        ("slow.demo.localhost", 404),
        ("yes.sync.localhost", 200),
        ("no.sync.localhost", 404),
        ("yes.plain.localhost", 404),  # an origin module without serves()
        ("yes.other.localhost", 404),  # no origin module
        ("yes.missing.localhost", 404),
        ("yes.demo.example.org", 404),
        ("", 404),
    ],
)
def test_ask_answers_200_only_for_a_host_its_plugins_serves_says_it_serves(
    tmp_path: Path,
    domain: str,
    status: int,
) -> None:
    """Caddy's on-demand TLS check gets 200 only while ``serves`` returns True, sync or async, within the deadline."""
    _write(tmp_path, "demo", origin=ORIGIN)
    _write(
        tmp_path,
        "sync",
        origin=ORIGIN.replace("async def serves", "def serves").replace("await asyncio.sleep(60)", "pass"),
    )
    _write(tmp_path, "plain", origin=ORIGIN.split("async def serves", maxsplit=1)[0])
    _write(tmp_path, "other", routes=ROUTES)
    client = _client(_api_app(tmp_path, "demo", "sync", "plain", "other"), "127.0.0.1:9100")

    with patch("mindroom.api.plugin_origins._SERVES_TIMEOUT_SECONDS", 0.2), capture_logs() as logs:
        answer = client.get("/ask", params={"domain": domain})

    assert answer.status_code == status
    assert answer.headers["cache-control"] == "no-store"
    failed = [log for log in logs if log["event"] == "Plugin origin serves() failed"]
    assert [log["plugin_name"] for log in failed] == (
        ["demo"] if domain.split(".", maxsplit=1)[0] in {"boom", "slow"} else []
    )


def test_ask_is_answered_only_off_plugin_hosts(tmp_path: Path) -> None:
    """On a plugin host, ``/ask`` is the plugin's own path, and the ask endpoint answers only GET."""
    _write(tmp_path, "demo", origin=ORIGIN)
    app = _api_app(tmp_path, "demo")

    assert _client(app, "x.demo.localhost").get("/ask", params={"domain": "yes.demo.localhost"}).status_code == 404
    assert _client(app, "localhost").post("/ask", params={"domain": "yes.demo.localhost"}).status_code == 404
    assert _client(app, "localhost").get("/ask", params={"domain": "yes.demo.localhost"}).status_code == 200


def test_plugin_origin_url_names_the_calling_plugins_host(tmp_path: Path) -> None:
    """An API route links to its own plugin's host, and answers 503 while origins are off."""
    _write(tmp_path, "demo", routes=ROUTES)

    on = TestClient(_api_app(tmp_path, "demo")).get("/api/plugins/demo/link")
    off = TestClient(_api_app(tmp_path, "demo", MINDROOM_PLUGIN_ORIGIN_PORT="")).get("/api/plugins/demo/link")

    assert (on.status_code, on.json()) == (200, {"url": "http://item-1.demo.localhost:9100"})
    assert (off.status_code, off.json()) == (503, {"detail": "Plugin origins are off."})


@pytest.mark.parametrize(
    ("env", "url"),
    [
        ({}, "http://a.demo.localhost:9100"),
        ({"MINDROOM_PLUGIN_ORIGIN_DOMAIN": "apps.localhost"}, "http://a.demo.apps.localhost:9100"),
        ({"MINDROOM_PLUGIN_ORIGIN_PUBLIC_PORT": "8080"}, "http://a.demo.localhost:8080"),
        ({"MINDROOM_PLUGIN_ORIGIN_DOMAIN": "Apps.Example.org."}, "https://a.demo.apps.example.org"),
        (
            {"MINDROOM_PLUGIN_ORIGIN_DOMAIN": "apps.example.org", "MINDROOM_PLUGIN_ORIGIN_PUBLIC_PORT": "8443"},
            "https://a.demo.apps.example.org:8443",
        ),
        ({"MINDROOM_PLUGIN_ORIGIN_PORT": ""}, None),
    ],
)
def test_plugin_origin_urls(tmp_path: Path, env: dict[str, str], url: str | None) -> None:
    """URLs are ``https`` except on ``localhost``, and carry the listener's own port only there."""
    assert plugin_origin_url(_paths(tmp_path, **env), "demo", "a") == url


@pytest.mark.parametrize("label", ["", "a.b", "A", "-a", "a_b", "a" * 64])
def test_a_label_is_one_dns_label(tmp_path: Path, label: str) -> None:
    """A label that is not one lowercase DNS label is refused, so it never names another host."""
    with pytest.raises(ValueError, match="one DNS label"):
        plugin_origin_url(_paths(tmp_path), "demo", label)


def test_a_plugin_whose_name_is_no_dns_label_cannot_have_an_origin_module(tmp_path: Path) -> None:
    """``mindroom plugins check`` says why; the same name without an origin module loads."""
    root = _write(tmp_path, "my_plugin", origin=ORIGIN)

    with pytest.raises(PluginValidationError, match="'my_plugin' cannot serve origins"):
        check_plugin(root)

    (root / "mindroom.plugin.json").write_text(json.dumps({"name": "my_plugin"}), encoding="utf-8")
    check_plugin(root)


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("x = 1\n", "Plugin origin module must define router: APIRouter"),
        ("from fastapi import APIRouter\nrouter = APIRouter()\nserves = 1\n", "serves must be callable"),
    ],
)
def test_a_broken_origin_module_costs_only_the_origins(tmp_path: Path, source: str, error: str) -> None:
    """Its routes still serve and its hosts answer 404; only the check fails."""
    root = _write(tmp_path, "demo", origin=source, routes=ROUTES)

    with pytest.raises(PluginValidationError, match=error):
        check_plugin(root)
    app = _api_app(tmp_path, "demo")
    assert TestClient(app).get("/api/plugins/demo/link").status_code == 200
    assert _client(app, "x.demo.localhost").get("/").status_code == 404
    assert _client(app, "localhost").get("/ask", params={"domain": "yes.demo.localhost"}).status_code == 404


@pytest.mark.asyncio
async def test_without_its_port_nothing_listens(tmp_path: Path) -> None:
    """With ``MINDROOM_PLUGIN_ORIGIN_PORT`` unset, no listener starts."""
    with patch("mindroom.api.plugin_origins.serve_listener", side_effect=AssertionError("listener started")):
        async with serve_plugin_origins(
            _paths(tmp_path, MINDROOM_PLUGIN_ORIGIN_PORT=""),
            host="127.0.0.1",
            api_app=FastAPI(),
            log_level="INFO",
        ):
            pass


@pytest.mark.asyncio
async def test_a_reload_closes_the_replaced_plugins_origin_websockets(tmp_path: Path) -> None:
    """The listener serves an origin websocket, which closes with 1012 once a reload replaces its plugin."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    _write(tmp_path, "demo", origin=ORIGIN)
    app = _api_app(tmp_path, "demo", MINDROOM_PLUGIN_ORIGIN_PORT=str(port))
    snapshot = config_lifecycle.require_api_state(app).snapshot
    assert snapshot.runtime_config is not None
    config = snapshot.runtime_config
    lifespans = PluginLifespans(snapshot.runtime_paths, lambda: config)
    await lifespans.apply_published()
    url = f"ws://127.0.0.1:{port}/live"

    async with (
        serve_plugin_origins(snapshot.runtime_paths, host="127.0.0.1", api_app=app, log_level="INFO"),
        aiohttp.ClientSession() as client,
    ):
        websocket = await client.ws_connect(url, headers={"Host": "x.demo.localhost"})
        assert await websocket.receive_str(timeout=5) == "v1"

        _write(tmp_path, "demo", origin=ORIGIN.replace("VERSION = 1", "VERSION = 2"))
        publish_test_plugins(config, snapshot.runtime_paths)
        await lifespans.apply_published()

        message = await websocket.receive(timeout=5)
        assert (message.type, message.data) == (aiohttp.WSMsgType.CLOSE, 1012)
        websocket = await client.ws_connect(url, headers={"Host": "x.demo.localhost"})
        assert await websocket.receive_str(timeout=5) == "v2"
        await websocket.close()
        assert config_plugins(config, snapshot.runtime_paths).loaded_plugins[0].origin_app is not None
    await lifespans.stop()
