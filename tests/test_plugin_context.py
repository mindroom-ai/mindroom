"""A plugin's routes and tools read their own plugin's settings, state directory, and config through its context."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from mindroom.api.app_state import ensure_app_state
from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.message_target import MessageTarget
from mindroom.plugin_api import PluginContext, current_plugin_context
from mindroom.plugin_http import plugin_context
from mindroom.tool_system.plugins import PluginInactiveError
from mindroom.tool_system.runtime_context import (
    DetachedRequesterContext,
    ToolRuntimeContext,
    detached_requester_context,
    tool_runtime_context,
)
from tests.authorization_helpers import isolated_membership_index
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup, publish_test_plugins
from tests.test_plugin_routes import _app, _write_plugin

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

ROUTES = """
from typing import Annotated

from fastapi import APIRouter, Depends, Request, WebSocket

from mindroom.plugin_api import PluginContext
from mindroom.plugin_http import plugin_context

router = APIRouter()
Context = Annotated[PluginContext, Depends(plugin_context)]


@router.get("/context")
def context(ctx: Context) -> dict:
    try:
        ctx.settings["greeting"] = "changed"
    except TypeError:
        read_only = True
    else:
        read_only = False
    return {
        "plugin_name": ctx.plugin_name,
        "settings": dict(ctx.settings),
        "read_only": read_only,
        "plugins": [entry.path for entry in ctx.config.plugins],
        "storage_root": str(ctx.runtime_paths.storage_root),
    }


@router.post("/state")
def state(ctx: Context) -> dict:
    (ctx.state_root / "note.txt").write_text(ctx.plugin_name)
    return {"state_root": str(ctx.state_root)}


@router.get("/reload-then-read")
def reload_then_read(request: Request, ctx: Context) -> dict:
    request.app.state.reload()
    return {"active": ctx.is_active(), "settings": dict(ctx.settings)}


@router.websocket("/live-dependency")
async def live_dependency(websocket: WebSocket, ctx: Context) -> None:
    await websocket.accept()
    await websocket.send_json(dict(ctx.settings))
    await websocket.close()


@router.websocket("/live")
async def live(websocket: WebSocket) -> None:
    await websocket.accept()
    connected = plugin_context(websocket)
    await websocket.send_json(dict(connected.settings))
    while True:
        message = await websocket.receive_text()
        ctx = connected if message == "stale" else plugin_context(websocket)
        await websocket.send_json(dict(ctx.settings))
"""


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )


def _bound(runtime_paths: RuntimePaths, *entries: dict[str, Any]) -> Config:
    """Return a config whose ``plugins:`` are ``entries``, each naming a plugin under ``plugins/`` by its ``name``."""
    plugins = [{"path": f"./plugins/{entry.pop('name')}", **entry} for entry in map(dict, entries)]
    return bind_runtime_paths(Config(plugins=plugins), runtime_paths)


def _client(tmp_path: Path, *entries: dict[str, Any]) -> tuple[FastAPI, TestClient, RuntimePaths]:
    """Publish the plugins ``entries`` name, each serving ``ROUTES``, and return the API serving them."""
    for entry in entries:
        _write_plugin(tmp_path, ROUTES, name=entry["name"])
    runtime_paths = _runtime_paths(tmp_path)
    config = _bound(runtime_paths, *entries)
    publish_test_plugins(config, runtime_paths)
    app = _app(config, runtime_paths)
    return app, TestClient(app), runtime_paths


def _reload(app: FastAPI, config: Config, runtime_paths: RuntimePaths | None = None) -> None:
    """Publish ``config`` to the API as a config reload does, for other ``runtime_paths`` when given."""
    state = ensure_app_state(app).api_state
    assert state is not None
    snapshot = state.snapshot
    state.snapshot = ApiSnapshot(
        snapshot.generation + 1,
        runtime_paths or snapshot.runtime_paths,
        runtime_config=config,
    )


def test_each_plugin_route_reads_its_own_plugins_settings_and_config(tmp_path: Path) -> None:
    """Two plugins' routes each get their own entry's settings, read-only, with the config and paths they run under."""
    _, client, runtime_paths = _client(
        tmp_path,
        {"name": "demo", "settings": {"greeting": "hello", "nested": {"n": 1}}},
        {"name": "other", "settings": {"greeting": "hi"}},
    )

    demo = client.get("/api/plugins/demo/context").json()
    other = client.get("/api/plugins/other/context").json()

    assert demo == {
        "plugin_name": "demo",
        "settings": {"greeting": "hello", "nested": {"n": 1}},
        "read_only": True,
        "plugins": ["./plugins/demo", "./plugins/other"],
        "storage_root": str(runtime_paths.storage_root),
    }
    assert (other["plugin_name"], other["settings"], other["read_only"]) == ("other", {"greeting": "hi"}, True)


def test_state_root_is_one_private_directory_per_plugin_created_on_first_use(tmp_path: Path) -> None:
    """Each plugin's state root is its own directory under the storage root, mode 0700, made when first used."""
    _, client, runtime_paths = _client(tmp_path, {"name": "demo"}, {"name": "other"})
    plugins_root = runtime_paths.storage_root / "plugins"
    # A hook's state root made the directory before, with the default mode.
    (plugins_root / "other").mkdir(parents=True, mode=0o755)
    (plugins_root / "other").chmod(0o755)
    client.get("/api/plugins/demo/context")
    assert not (plugins_root / "demo").exists()

    demo = client.post("/api/plugins/demo/state").json()["state_root"]
    other = client.post("/api/plugins/other/state").json()["state_root"]

    assert (demo, other) == (str(plugins_root / "demo"), str(plugins_root / "other"))
    for name in ("demo", "other"):
        assert (plugins_root / name).stat().st_mode & 0o777 == 0o700
        assert (plugins_root / name / "note.txt").read_text() == name


@pytest.mark.parametrize("change", ["disabled", "removed", "other-runtime"])
def test_a_reload_that_disables_or_removes_the_plugin_fails_its_route_closed(tmp_path: Path, change: str) -> None:
    """A request that holds its plugin's context when a reload disables or removes the plugin answers 503.

    So does one whose API moved to another runtime, even with a config that would load the plugin.
    """
    app, client, runtime_paths = _client(tmp_path, {"name": "demo", "settings": {"key": "secret"}})
    if change == "other-runtime":
        config = _bound(runtime_paths, {"name": "demo", "settings": {"key": "secret"}})
        (tmp_path / "other").mkdir()
        app.state.reload = lambda: _reload(app, config, _runtime_paths(tmp_path / "other"))
    else:
        entries = [{"name": "demo", "enabled": False}] if change == "disabled" else []
        app.state.reload = lambda: _reload(app, _bound(runtime_paths, *entries))

    response = client.get("/api/plugins/demo/reload-then-read")

    assert response.status_code == 503
    assert response.json() == {"detail": "Plugin 'demo' is not active: the current config does not load it."}
    assert "secret" not in response.text


def test_a_reload_that_keeps_the_plugin_leaves_its_context_active(tmp_path: Path) -> None:
    """A context keeps the settings of the config it was made under while a reload keeps the plugin loaded."""
    app, client, runtime_paths = _client(tmp_path, {"name": "demo", "settings": {"key": "old"}})
    app.state.reload = lambda: _reload(app, _bound(runtime_paths, {"name": "demo", "settings": {"key": "new"}}))

    response = client.get("/api/plugins/demo/reload-then-read")

    assert response.json() == {"active": True, "settings": {"key": "old"}}


def test_a_websocket_reads_its_plugins_settings_again_after_a_reload(tmp_path: Path) -> None:
    """A connected websocket that asks for its context again sees a reload's settings, and is closed once disabled."""
    app, client, runtime_paths = _client(tmp_path, {"name": "demo", "settings": {"key": "old"}})

    with client.websocket_connect("/api/plugins/demo/live-dependency") as websocket:
        assert websocket.receive_json() == {"key": "old"}
    with client.websocket_connect("/api/plugins/demo/live") as websocket:
        assert websocket.receive_json() == {"key": "old"}
        _reload(app, _bound(runtime_paths, {"name": "demo", "settings": {"key": "new"}}))
        websocket.send_text("again")
        assert websocket.receive_json() == {"key": "new"}
        websocket.send_text("stale")
        assert websocket.receive_json() == {"key": "old"}

        _reload(app, _bound(runtime_paths, {"name": "demo", "enabled": False}))
        websocket.send_text("stale")
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()

    assert closed.value.code == 1013
    assert closed.value.reason == "Plugin 'demo' is not active: the current config does not load it."


def test_plugin_context_outside_a_plugin_route_names_its_misuse() -> None:
    """A request no plugin's routes serve has no plugin context, which the error says rather than a failed reload."""
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

    with pytest.raises(RuntimeError, match=r"plugin_context\(\) serves only a plugin's own routes"):
        plugin_context(request)


def test_a_plugin_tool_reads_its_plugins_context_until_a_reload_disables_it(tmp_path: Path) -> None:
    """Inside a tool call a plugin reads its context by name, and it fails closed once the current config drops it."""
    _write_plugin(tmp_path, None, name="demo")
    runtime_paths = _runtime_paths(tmp_path)
    config = _bound(runtime_paths, {"name": "demo", "settings": {"key": "value"}})
    plugins = publish_test_plugins(config, runtime_paths)
    current = [config]
    runtime = ToolRuntimeContext(
        agent_name="code",
        target=MessageTarget.resolve(room_id="!room:example.org", thread_id=None, reply_to_event_id=None),
        requester_id="@alice:example.org",
        client=MagicMock(),
        config=config,
        runtime_paths=runtime_paths,
        plugins=plugins,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        config_provider=lambda: current[0],
    )

    with tool_runtime_context(runtime):
        ctx = current_plugin_context("demo")
        with pytest.raises(PluginInactiveError, match="Plugin 'nope' is not active"):
            current_plugin_context("nope")

    assert isinstance(ctx, PluginContext)
    assert (ctx.plugin_name, dict(ctx.settings), ctx.config, ctx.is_active()) == (
        "demo",
        {"key": "value"},
        config,
        True,
    )
    current[0] = _bound(runtime_paths, {"name": "demo", "enabled": False})
    assert not ctx.is_active()
    for member in ("settings", "config", "state_root"):
        with pytest.raises(PluginInactiveError, match="Plugin 'demo' is not active"):
            getattr(ctx, member)
    assert not (runtime_paths.storage_root / "plugins" / "demo").exists()
    with pytest.raises(RuntimeError, match="needs a tool call in a Matrix conversation or an OpenAI-compatible API"):
        current_plugin_context("demo")


def test_a_plugin_tool_reads_its_plugins_context_in_an_openai_compatible_request(tmp_path: Path) -> None:
    """A tool call without a Matrix conversation, as for an OpenAI-compatible API request, reads its plugin's context."""
    _write_plugin(tmp_path, None, name="demo")
    runtime_paths = _runtime_paths(tmp_path)
    config = _bound(runtime_paths, {"name": "demo", "settings": {"key": "value"}})
    publish_test_plugins(config, runtime_paths)
    current: list[Config | None] = [config]
    authority = DetachedRequesterContext(
        requester_id="@alice:example.org",
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=isolated_membership_index(),
        config_provider=lambda: current[0],
        budget_monitor=None,
    )

    with detached_requester_context(authority):
        ctx = current_plugin_context("demo")

    assert (dict(ctx.settings), ctx.config, ctx.is_active()) == ({"key": "value"}, config, True)
    current[0] = None
    assert not ctx.is_active()
