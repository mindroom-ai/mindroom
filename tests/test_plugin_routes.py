"""Plugins serve HTTP routes under /api/plugins/<plugin>/, from whichever plugins are published now."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from mindroom.api import config_lifecycle, main
from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import _client_session_store
from mindroom.api.plugin_routes import mount_plugin_routes
from mindroom.api.validation_errors import validation_error_response
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.plugin_check import check_plugin
from mindroom.tool_system.plugin_imports import PluginValidationError
from mindroom.tool_system.plugins import dispose_plugins, prepare_plugins, refresh_plugins
from tests.conftest import bind_runtime_paths, publish_test_plugins

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:example.org"
ALICE = "@alice:example.org"

ROUTES = """
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from mindroom.plugin_http import ClientUser, require_room_member

router = APIRouter()


class Echo(BaseModel):
    text: str


@router.get("/ping")
async def ping() -> dict:
    return {"pong": True}


@router.post("/echo")
async def echo(body: Echo) -> dict:
    return {"echo": body.text}


@router.get("/boom")
async def boom() -> dict:
    raise RuntimeError("plugin secret in a traceback")


@router.get("/where")
async def where(request: Request) -> dict:
    return {"url_path": request.url.path, "root_path": request.scope["root_path"]}


@router.get("/items/{item_id}", name="plugin_item")
async def item(item_id: str) -> dict:
    return {"item": item_id}


@router.get("/link")
async def link(request: Request) -> dict:
    return {"url": str(request.url_for("plugin_item", item_id="123"))}


@router.get("/rooms/{room_id}/me")
async def me(user: Annotated[ClientUser, Depends(require_room_member)]) -> dict:
    return {"user_id": user.user_id}


@router.get("/rooms/{room_id}/topic")
async def topic(room_id: str, request: Request) -> dict:
    user = await require_room_member(request, room_id)
    return {"user_id": user.user_id, "room_id": room_id}


@router.get("/rooms/{room_id}/secret", dependencies=[Depends(require_room_member)])
async def secret(room_id: str) -> dict:
    return {"secret": room_id}
"""

# FastAPI refuses a route at "" only as a router is included in an app, which MindRoom does as the plugin loads.
EMPTY_PATH_ROUTES = f"""{ROUTES}

@router.get("")
async def root() -> dict:
    return {{"root": True}}
"""


TOOLS = """
from agno.tools import Toolkit

from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess
from mindroom.tool_system.registration import register_tool_with_metadata


class DemoTool(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="demo", tools=[])


@register_tool_with_metadata(
    name="demo_routes_tool",
    display_name="Demo",
    description="A tool beside the plugin's routes",
    file_access=ToolFileAccess.NONE,
    category=ToolCategory.DEVELOPMENT,
)
def demo_tools():
    return DemoTool
"""


def _write_plugin(
    tmp_path: Path,
    routes_source: str | None,
    *,
    name: str = "demo",
    tools_source: str | None = None,
) -> Path:
    """Write plugin ``name`` under ``tmp_path/plugins`` with the routes and tools modules given; return its root."""
    root = tmp_path / "plugins" / name
    root.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, str] = {"name": name}
    for kind, source in (("routes", routes_source), ("tools", tools_source)):
        if source is not None:
            manifest[f"{kind}_module"] = f"{kind}.py"
            (root / f"{kind}.py").write_text(source, encoding="utf-8")
    (root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _config(tmp_path: Path, *names: str) -> tuple[Config, RuntimePaths]:
    """Return a config naming the plugins ``names`` under ``tmp_path/plugins``, bound to its runtime paths."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    config = bind_runtime_paths(Config(plugins=[f"./plugins/{name}" for name in names or ("demo",)]), runtime_paths)
    return config, runtime_paths


def _api_state(config: Config, runtime_paths: RuntimePaths) -> config_lifecycle.ApiState:
    return config_lifecycle.ApiState(
        threading.Lock(),
        config_lifecycle.ApiSnapshot(1, runtime_paths, runtime_config=config),
    )


def _app(config: Config, runtime_paths: RuntimePaths) -> FastAPI:
    """Return an API app serving ``config`` with the plugin routes mounted, answering errors as the main app does."""
    app = FastAPI(exception_handlers={RequestValidationError: validation_error_response})
    ensure_app_state(app).api_state = _api_state(config, runtime_paths)
    mount_plugin_routes(app)
    return app


def _published_client(tmp_path: Path, routes_source: str = ROUTES) -> tuple[FastAPI, TestClient]:
    _write_plugin(tmp_path, routes_source)
    config, runtime_paths = _config(tmp_path)
    publish_test_plugins(config, runtime_paths)
    app = _app(config, runtime_paths)
    return app, TestClient(app)


def test_plugin_get_route_served(tmp_path: Path) -> None:
    """A GET route of the plugin's router answers under the plugin's name."""
    _, client = _published_client(tmp_path)

    response = client.get("/api/plugins/demo/ping")

    assert response.status_code == 200
    assert response.json() == {"pong": True}


def test_plugin_post_route_served(tmp_path: Path) -> None:
    """A POST route gets its JSON body."""
    _, client = _published_client(tmp_path)

    response = client.post("/api/plugins/demo/echo", json={"text": "hi"})

    assert response.status_code == 200
    assert response.json() == {"echo": "hi"}


def test_plugin_validation_errors_leave_out_input(tmp_path: Path) -> None:
    """A rejected body is answered like the main API's, without echoing the rejected value."""
    _, client = _published_client(tmp_path)

    response = client.post("/api/plugins/demo/echo", json={"text": ["secret"]})

    assert response.status_code == 422
    assert "secret" not in response.text
    assert all("input" not in error for error in response.json()["detail"])


def test_plugin_route_sees_its_mount_path(tmp_path: Path) -> None:
    """Inside a plugin route the URL is the full one and the root path ends at the plugin's name."""
    _, client = _published_client(tmp_path)

    response = client.get("/api/plugins/demo/where")

    assert response.json() == {"url_path": "/api/plugins/demo/where", "root_path": "/api/plugins/demo"}


def test_unknown_plugin_404(tmp_path: Path) -> None:
    """A plugin that is not loaded, that has no routes, or a path its router lacks answers 404, to a websocket too."""
    _write_plugin(tmp_path, ROUTES)
    _write_plugin(tmp_path, None, name="plain")
    config, runtime_paths = _config(tmp_path, "demo", "plain")
    publish_test_plugins(config, runtime_paths)
    client = TestClient(_app(config, runtime_paths))

    for path in ("/api/plugins/nope/ping", "/api/plugins/plain/ping", "/api/plugins/", "/api/plugins/demo/nope"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "Not Found"}, path
    with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect("/api/plugins/nope/ws"):
        pass
    assert denied.value.status_code == 404
    assert denied.value.json() == {"detail": "Not Found"}


def test_raising_route_answers_an_opaque_500_and_the_app_keeps_serving(tmp_path: Path) -> None:
    """A plugin route that raises gives a 500 that names nothing of the error, and the next request is served."""
    app, _ = _published_client(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/api/plugins/demo/boom")

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert client.get("/api/plugins/demo/ping").json() == {"pong": True}


@pytest.mark.parametrize(
    ("routes_source", "error"),
    [
        ("ROUTER = None\n", r"Plugin routes module must define router: APIRouter"),
        (EMPTY_PATH_ROUTES, r"Plugin router cannot be served: Prefix and path cannot be both empty"),
    ],
    ids=["no-router", "empty-path"],
)
def test_unservable_routes_module_costs_only_the_routes(tmp_path: Path, routes_source: str, error: str) -> None:
    """A router that can't be served costs a reload only the plugin's routes, and the plugin check fails it."""
    root = _write_plugin(tmp_path, ROUTES, tools_source=TOOLS)
    config, runtime_paths = _config(tmp_path)
    publish_test_plugins(config, runtime_paths)
    client = TestClient(_app(config, runtime_paths))
    assert client.get("/api/plugins/demo/ping").status_code == 200

    _write_plugin(tmp_path, routes_source, tools_source=TOOLS)
    [plugin] = publish_test_plugins(config, runtime_paths).loaded_plugins

    assert (set(plugin.tools), plugin.routes_app) == ({"demo_routes_tool"}, None)
    assert client.get("/api/plugins/demo/ping").status_code == 404
    with pytest.raises(PluginValidationError, match=error):
        check_plugin(root)


def test_reload_serves_new_router(tmp_path: Path) -> None:
    """After the plugin is published again, the same app serves the new router's code."""
    _write_plugin(tmp_path, ROUTES)
    config, runtime_paths = _config(tmp_path)
    publish_test_plugins(config, runtime_paths)
    client = TestClient(_app(config, runtime_paths))
    assert client.get("/api/plugins/demo/ping").json() == {"pong": True}

    _write_plugin(tmp_path, ROUTES.replace('{"pong": True}', '{"pong": 2}'))
    publish_test_plugins(config, runtime_paths)

    assert client.get("/api/plugins/demo/ping").json() == {"pong": 2}


def test_plugin_route_generates_urls_to_its_own_routes(tmp_path: Path) -> None:
    """``request.url_for`` in a plugin route finds the plugin's own routes under its mount path, after a reload too.

    Behind a proxy that serves MindRoom under a root path, the URL keeps that path.
    """
    _write_plugin(tmp_path, ROUTES)
    config, runtime_paths = _config(tmp_path)
    publish_test_plugins(config, runtime_paths)
    app = _app(config, runtime_paths)
    client = TestClient(app)
    assert client.get("/api/plugins/demo/link").json() == {"url": "http://testserver/api/plugins/demo/items/123"}

    _write_plugin(tmp_path, ROUTES.replace('"/items/{item_id}"', '"/things/{item_id}"'))
    publish_test_plugins(config, runtime_paths)

    assert client.get("/api/plugins/demo/link").json() == {"url": "http://testserver/api/plugins/demo/things/123"}
    proxied = TestClient(app, root_path="/mindroom").get("/mindroom/api/plugins/demo/link")
    assert proxied.json() == {"url": "http://testserver/mindroom/api/plugins/demo/things/123"}


def test_worker_load_skips_routes_module(tmp_path: Path) -> None:
    """A worker's load never imports the routes module, which the primary's load does."""
    _write_plugin(tmp_path, "raise RuntimeError('routes module imported')\n")
    config, runtime_paths = _config(tmp_path)

    [plugin] = refresh_plugins(config, runtime_paths).loaded_plugins

    assert (plugin.name, plugin.routes_app) == ("demo", None)
    generation = prepare_plugins(config, runtime_paths)
    dispose_plugins(generation)
    [plugin] = generation.plugins.values()
    assert "routes module imported" in str(plugin.routes_error)


def test_plugin_route_reads_the_main_apps_sessions_and_membership(tmp_path: Path) -> None:
    """Plugin routes see the main app's client sessions and room membership, so their checks pass and refuse."""
    app, client = _published_client(tmp_path)
    token = _client_session_store(app).create(ALICE).token

    async def room_members(room_id: str) -> frozenset[str]:
        return frozenset({ALICE}) if room_id == ROOM else frozenset()

    ensure_app_state(app).room_members = room_members
    bearer = {"Authorization": f"Bearer {token}"}

    def me(room_id: str, headers: dict[str, str]) -> tuple[int, object]:
        response = client.get(f"/api/plugins/demo/rooms/{quote(room_id, safe='')}/me", headers=headers)
        return response.status_code, response.json()

    assert me(ROOM, bearer) == (200, {"user_id": ALICE})
    assert me(ROOM, {})[0] == 401
    assert me("!other:example.org", bearer) == (403, {"detail": "Not a member of this room."})
    topic = client.get(f"/api/plugins/demo/rooms/{quote(ROOM, safe='')}/topic", headers=bearer)
    assert (topic.status_code, topic.json()) == (200, {"user_id": ALICE, "room_id": ROOM})


def test_room_member_dependency_cannot_be_told_whose_membership_to_check(tmp_path: Path) -> None:
    """A ``user_id`` in the query string never picks the user: the check is always of the session's own user."""
    app, client = _published_client(tmp_path)
    bob = {"Authorization": f"Bearer {_client_session_store(app).create('@bob:example.org').token}"}

    async def room_members(room_id: str) -> frozenset[str]:
        return frozenset({ALICE}) if room_id == ROOM else frozenset()

    ensure_app_state(app).room_members = room_members
    room = quote(ROOM, safe="")
    for path in ("me", "topic", "secret"):
        url = f"/api/plugins/demo/rooms/{room}/{path}?user_id={quote(ALICE)}"
        assert client.get(url).status_code == 401, path
        assert client.get(url, headers=bob).status_code == 403, path


def test_frontend_catch_all_does_not_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """On the real API app, a plugin's GET route wins over the frontend's catch-all.

    A route that raises answers its opaque 500 from inside the main app's middleware, so it keeps the forced headers.
    """
    _write_plugin(tmp_path, ROUTES)
    config, runtime_paths = _config(tmp_path)
    publish_test_plugins(config, runtime_paths)
    monkeypatch.setattr(ensure_app_state(main.app), "api_state", _api_state(config, runtime_paths))
    client = TestClient(main.app, raise_server_exceptions=False)

    response = client.get("/api/plugins/demo/ping")

    assert response.status_code == 200
    assert response.json() == {"pong": True}
    boom = client.get("/api/plugins/demo/boom")
    assert (boom.status_code, boom.text) == (500, "Internal Server Error")
    assert (boom.headers["cache-control"], boom.headers["x-content-type-options"]) == ("no-store", "nosniff")
