"""A plugin's lifespan starts once the primary publishes the plugin, and stops when a reload replaces it or MindRoom stops."""

from __future__ import annotations

import asyncio
import contextvars
import re
import socket
import sys
import uuid
from contextlib import asynccontextmanager
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
import uvicorn
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from mindroom import plugin_lifecycle
from mindroom.config.main import Config
from mindroom.hooks import EVENT_MESSAGE_RECEIVED
from mindroom.orchestration.config_updates import ConfigUpdatePlan
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.plugin_check import check_plugin
from mindroom.plugin_lifecycle import PluginLifespans
from mindroom.tool_system.plugin_imports import PluginValidationError
from mindroom.tool_system.plugins import (
    candidate_tool_registry,
    config_plugins,
    dispose_plugins,
    prepare_plugins,
    publish_plugins,
    refresh_plugins,
)
from tests.conftest import _TEST_PROBE_PREFIX, bind_runtime_paths, cached_plugin_module, test_runtime_paths
from tests.test_plugin_routes import _app, _write_plugin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastapi import FastAPI

    from mindroom.constants import RuntimePaths

# Records each start and stop, with the plugin's version, what process_state held, and the settings it read.
LIFECYCLE = """
import sys
from contextlib import asynccontextmanager

_probe = sys.modules[{probe!r}]
_probe.imports.append(({name!r}, {version}))


@asynccontextmanager
async def lifespan(lifecycle):
    state = lifecycle.process_state
    settings = dict(lifecycle.context().settings)
    _probe.events.append(("start", lifecycle.plugin_name, {version}, state.get("version"), settings))
    state["version"] = {version}
    yield
    _probe.events.append(("stop", lifecycle.plugin_name, {version}, lifecycle.stop_reason))
"""

ROUTES = """
import sys

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

_probe = sys.modules[{probe!r}]
router = APIRouter()


@router.get("/ping")
async def ping() -> dict:
    return {{"version": {version}}}


@router.websocket("/live")
async def live(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text("v{version}")
    try:
        while True:
            text = await websocket.receive_text()
            await websocket.send_text(f"v{version}:{{text}}")
    except WebSocketDisconnect as closed:
        _probe.events.append(("closed", {version}, closed.code))
    try:
        await websocket.send_text("late")
    except WebSocketDisconnect as refused:
        _probe.events.append(("late send", {version}, refused.code))
"""

TOOLS = """
from agno.tools import Toolkit

from mindroom.hooks import hook
from mindroom.tool_system.declarations import ToolCategory, ToolFileAccess
from mindroom.tool_system.registration import register_tool_with_metadata


class DemoTool(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="demo", tools=[])


@register_tool_with_metadata(
    name="demo_lifecycle_tool",
    display_name="Demo",
    description="A tool beside the plugin's lifespan",
    file_access=ToolFileAccess.NONE,
    category=ToolCategory.DEVELOPMENT,
)
def demo_tools():
    return DemoTool


@hook("message:received")
async def audit(ctx):
    del ctx
"""


def _probe() -> Any:  # noqa: ANN401 - a module the test gives the attributes its plugins read
    """Return a module the plugins a test writes record what they do in; the autouse plugin restore removes it."""
    probe = ModuleType(f"{_TEST_PROBE_PREFIX}{uuid.uuid4().hex}")
    probe.events = []
    probe.imports = []
    sys.modules[probe.__name__] = probe
    return probe


def _write(
    tmp_path: Path,
    probe: Any,  # noqa: ANN401
    name: str,
    version: int = 1,
    *,
    lifecycle: str = LIFECYCLE,
    routes: str | None = None,
    tools: str | None = None,
) -> Path:
    """Write plugin ``name`` as of ``version``, with a lifecycle module and the routes and tools given; return its root."""
    fields = {"probe": probe.__name__, "name": name, "version": version}
    routes_source = routes.format(**fields) if routes is not None else None
    root = _write_plugin(tmp_path, routes_source, name=name, tools_source=tools, lifecycle_module="lifecycle.py")
    (root / "lifecycle.py").write_text(lifecycle.format(**fields), encoding="utf-8")
    return root


def _config(runtime_paths: RuntimePaths, *names: str, settings: dict[str, Any] | None = None) -> Config:
    """Return a config enabling the plugins ``names`` under ``plugins/``, each with ``settings``."""
    plugins = [{"path": f"./plugins/{name}", "settings": settings or {}} for name in names]
    return bind_runtime_paths(Config(plugins=plugins), runtime_paths)


async def _publish(lifespans: PluginLifespans, config: Config, runtime_paths: RuntimePaths) -> None:
    """Load and publish ``config``'s plugins as the primary commits them, then run their lifespans."""
    generation = prepare_plugins(config, runtime_paths)
    publish_plugins(generation)
    await lifespans.apply_published()


async def _apply_plugin_change(orchestrator: _MultiAgentOrchestrator, current: Config, new: Config) -> None:
    """Apply a config reload from ``current`` to ``new`` that changes only plugin entries."""
    plan = ConfigUpdatePlan(new_config=new, plugin_changes=tuple(entry.path for entry in current.plugins))
    with (
        patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
    ):
        await orchestrator._apply_config_update_plan(current, plan)


@pytest.mark.asyncio
async def test_a_lifespan_runs_only_once_the_primary_publishes_its_plugin(tmp_path: Path) -> None:
    """Plugin checks, config validation, candidate loads, and worker refreshes never run a lifespan.

    Checks and validation import the lifecycle module as the primary does, and workers never import it.
    """
    probe = _probe()
    root = _write(tmp_path, probe, "demo", tools=TOOLS)
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "demo", settings={"greeting": "hi"})

    check_plugin(root)
    dispose_plugins(prepare_plugins(config, runtime_paths))
    candidate_tool_registry(config, runtime_paths)
    refresh_plugins(config, runtime_paths)

    assert probe.events == []
    assert probe.imports == [("demo", 1), ("demo", 1)]

    lifespans = PluginLifespans(runtime_paths, lambda: config)
    await _publish(lifespans, config, runtime_paths)
    await lifespans.stop()
    # A publication that comes after the shutdown stop starts nothing.
    await _publish(lifespans, config, runtime_paths)

    assert probe.events == [
        ("start", "demo", 1, None, {"greeting": "hi"}),
        ("stop", "demo", 1, "shutdown"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["start-raises", "lifecycle-module-removed"])
async def test_an_old_lifespan_no_new_one_took_over_from_stops_as_disabled(tmp_path: Path, change: str) -> None:
    """A reload whose new copy of the plugin runs no lifespan stops the old one as disabled, so it lets go of everything.

    A later reload or disable then has nothing left running that it could no longer stop.
    """
    probe = _probe()
    probe.phase = "start"
    _write(tmp_path, probe, "demo")
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "demo")
    lifespans = PluginLifespans(runtime_paths, lambda: config)
    await _publish(lifespans, config, runtime_paths)

    if change == "start-raises":
        _write(tmp_path, probe, "demo", 2, lifecycle=RAISING)
    else:
        _write_plugin(tmp_path, None, name="demo")
    await _publish(lifespans, config, runtime_paths)
    await lifespans.stop()

    assert probe.events[1:] == [("stop", "demo", 1, "disabled")]


@pytest.mark.asyncio
async def test_a_lifespan_runs_outside_the_context_of_the_reload_that_started_it(tmp_path: Path) -> None:
    """Context variables of the turn that reloads plugins, such as its log fields, never reach a lifespan."""
    lifecycle = """
import sys
from contextlib import asynccontextmanager

_probe = sys.modules[{probe!r}]


@asynccontextmanager
async def lifespan(lifecycle):
    _probe.events.append(_probe.turn.get())
    yield
"""
    probe = _probe()
    probe.turn = contextvars.ContextVar("turn", default=None)
    _write(tmp_path, probe, "demo", lifecycle=lifecycle)
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "demo")
    lifespans = PluginLifespans(runtime_paths, lambda: config)

    probe.turn.set("!reload-plugins")
    await _publish(lifespans, config, runtime_paths)
    await lifespans.stop()

    assert probe.events == [None]


@pytest.mark.asyncio
async def test_a_publication_its_caller_abandons_still_ends_in_the_shutdown_stop(tmp_path: Path) -> None:
    """A reload cancelled while a lifespan starts, as by shutdown, finishes, and shutdown then stops that lifespan."""
    lifecycle = """
import sys
from contextlib import asynccontextmanager

_probe = sys.modules[{probe!r}]


@asynccontextmanager
async def lifespan(lifecycle):
    _probe.starting.set()
    await _probe.gate.wait()
    _probe.events.append("start")
    yield
    _probe.events.append(("stop", lifecycle.stop_reason))
"""
    probe = _probe()
    probe.starting = asyncio.Event()
    probe.gate = asyncio.Event()
    _write(tmp_path, probe, "demo", lifecycle=lifecycle)
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "demo")
    lifespans = PluginLifespans(runtime_paths, lambda: config)

    publication = asyncio.create_task(_publish(lifespans, config, runtime_paths))
    await probe.starting.wait()
    publication.cancel()
    with pytest.raises(asyncio.CancelledError):
        await publication
    stopping = asyncio.create_task(lifespans.stop())
    await asyncio.sleep(0)
    probe.gate.set()
    await stopping

    assert probe.events == ["start", ("stop", "shutdown")]


@pytest.mark.asyncio
async def test_the_runtime_runs_each_plugins_lifespan_from_startup_through_reloads_to_shutdown(tmp_path: Path) -> None:
    """Startup starts each lifespan, and a reload starts the new ones before it stops the old ones.

    The new lifespan sees what the old one kept in process_state, and reads the new config's settings. A config reload
    that removes a plugin stops it as disabled, and shutdown stops the rest.
    """
    probe = _probe()
    for name in ("kept", "dropped"):
        _write(tmp_path, probe, name)
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "kept", "dropped", settings={"greeting": "hi"})
    orchestrator = _MultiAgentOrchestrator(runtime_paths, config=config)

    try:
        await orchestrator.initialize()
        assert probe.events == [
            ("start", "kept", 1, None, {"greeting": "hi"}),
            ("start", "dropped", 1, None, {"greeting": "hi"}),
        ]

        probe.events.clear()
        _write(tmp_path, probe, "kept", 2)
        orchestrator.running = True
        await orchestrator.reload_plugins_now(source="test")
        assert probe.events == [
            ("start", "kept", 2, 1, {"greeting": "hi"}),
            ("start", "dropped", 1, 1, {"greeting": "hi"}),
            ("stop", "kept", 1, "reload"),
            ("stop", "dropped", 1, "reload"),
        ]

        probe.events.clear()
        # A config reload that leaves plugins: as they are restarts no lifespan.
        same_plugins = _config(runtime_paths, "kept", "dropped", settings={"greeting": "hi"})
        with (
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        ):
            await orchestrator._apply_config_update_plan(config, ConfigUpdatePlan(new_config=same_plugins))
        assert probe.events == []

        new_config = _config(runtime_paths, "kept", settings={"greeting": "hello"})
        await _apply_plugin_change(orchestrator, same_plugins, new_config)
        assert probe.events == [
            ("start", "kept", 2, 2, {"greeting": "hello"}),
            ("stop", "kept", 2, "reload"),
            ("stop", "dropped", 1, "disabled"),
        ]
        probe.events.clear()
    finally:
        await orchestrator.stop()

    assert probe.events == [("stop", "kept", 2, "shutdown")]


@pytest.mark.asyncio
async def test_a_reload_cancels_no_task_the_plugin_keeps_in_its_process_state(tmp_path: Path) -> None:
    """A task in process_state outlives a reload even when module globals also hold it, its container, or the state.

    Tasks the module keeps anywhere else end.
    """
    lifecycle = """
import asyncio
from contextlib import asynccontextmanager

STATE = {{}}
TICKER = None
WORKERS = set()
OTHER = {{}}


@asynccontextmanager
async def lifespan(lifecycle):
    global STATE, TICKER, WORKERS
    STATE = lifecycle.process_state
    if "ticker" not in STATE:
        STATE["ticker"] = asyncio.create_task(asyncio.Event().wait())
        STATE["workers"] = {{asyncio.create_task(asyncio.Event().wait())}}
    TICKER = STATE["ticker"]
    WORKERS = STATE["workers"]
    OTHER["task"] = asyncio.create_task(asyncio.Event().wait())
    yield
"""
    probe = _probe()
    root = _write(tmp_path, probe, "demo", lifecycle=lifecycle)
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "demo")
    lifespans = PluginLifespans(runtime_paths, lambda: config)

    await _publish(lifespans, config, runtime_paths)
    module = cached_plugin_module((root / "lifecycle.py").resolve())
    ticker, [worker], other = module.STATE["ticker"], module.WORKERS, module.OTHER["task"]
    try:
        await _publish(lifespans, config, runtime_paths)
        await asyncio.sleep(0)

        assert other.cancelled()
        assert not ticker.done()
        assert not worker.done()
    finally:
        ticker.cancel()
        worker.cancel()
        await lifespans.stop()


HANGING = """
import asyncio
import sys
from contextlib import asynccontextmanager

_probe = sys.modules[{probe!r}]


async def _hang():
    # Ignores cancellation, so only the test's release ends it.
    while not _probe.release.is_set():
        try:
            await _probe.release.wait()
        except asyncio.CancelledError:
            pass


@asynccontextmanager
async def lifespan(lifecycle):
    if _probe.phase == "start":
        await _hang()
    yield
    await _hang()
"""

RAISING = """
import sys
from contextlib import asynccontextmanager

_probe = sys.modules[{probe!r}]


@asynccontextmanager
async def lifespan(lifecycle):
    if _probe.phase == "start":
        raise RuntimeError("lifespan broke")
    yield
    raise RuntimeError("lifespan broke")
"""

# Waits for the test's release, which it lets cancel it, then records that it went on.
SLOW = """
import sys
from contextlib import asynccontextmanager

_probe = sys.modules[{probe!r}]


@asynccontextmanager
async def lifespan(lifecycle):
    if _probe.phase == "start":
        await _probe.release.wait()
        _probe.events.append("went on")
    yield
    await _probe.release.wait()
    _probe.events.append("went on")
"""

# Awaits a task it cancelled without catching the CancelledError that raises, so the lifespan ends cancelled.
CANCELLED = """
import asyncio
import sys
from contextlib import asynccontextmanager

_probe = sys.modules[{probe!r}]


async def _await_cancelled():
    task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    task.cancel()
    await task


@asynccontextmanager
async def lifespan(lifecycle):
    if _probe.phase == "start":
        await _await_cancelled()
    yield
    await _await_cancelled()
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "stop"])
@pytest.mark.parametrize(
    ("failure", "lifecycle"),
    [("raises", RAISING), ("hangs", HANGING), ("slow", SLOW), ("cancelled", CANCELLED)],
    ids=["raises", "hangs", "slow", "cancelled"],
)
async def test_a_lifespan_that_raises_or_hangs_is_logged_and_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure: str,
    lifecycle: str,
) -> None:
    """A start or stop that raises or overruns its deadline is logged with the plugin's name and holds nothing up.

    One that overruns is cancelled, so it does not go on after MindRoom stopped waiting for it.
    """
    monkeypatch.setattr(plugin_lifecycle, "_LIFESPAN_DEADLINE_SECONDS", 0.5)
    probe = _probe()
    probe.phase = phase
    probe.release = asyncio.Event()
    _write(tmp_path, probe, "demo", lifecycle=lifecycle)
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "demo")
    lifespans = PluginLifespans(runtime_paths, lambda: config)

    try:
        with capture_logs() as logs:
            async with asyncio.timeout(5):
                await _publish(lifespans, config, runtime_paths)
                await lifespans.stop()
    finally:
        probe.release.set()
        for _ in range(3):
            await asyncio.sleep(0)

    assert probe.events == []
    errors = [log for log in logs if log["log_level"] == "error"]
    event = {"start": "Plugin lifespan failed to start", "stop": "Plugin lifespan failed to stop"}[phase]
    assert [(log["event"], log["plugin_name"]) for log in errors] == [(event, "demo")]
    if failure in {"raises", "cancelled"}:
        assert errors[0]["exc_info"] is True
    else:
        assert errors[0]["timeout_seconds"] == 0.5


# A lifecycle module that cannot load, with the reason MindRoom gives.
BROKEN_LIFECYCLE_MODULES = [
    pytest.param("raise RuntimeError('import broke')\n", "Plugin lifecycle module execution failed", id="import-error"),
    pytest.param("LIFESPAN = None\n", "Plugin lifecycle module must define lifespan(lifecycle)", id="no-lifespan"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lifecycle", "error"),
    [*BROKEN_LIFECYCLE_MODULES, pytest.param(RAISING, None, id="start-raises")],
)
async def test_a_broken_lifespan_costs_the_plugin_only_its_lifespan(
    tmp_path: Path,
    lifecycle: str,
    error: str | None,
) -> None:
    """The plugin's tools, hooks, and routes keep working; a broken lifecycle module is logged with the plugin's name."""
    probe = _probe()
    probe.phase = "start"
    _write(tmp_path, probe, "demo", lifecycle=lifecycle, routes=ROUTES, tools=TOOLS)
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "demo")
    lifespans = PluginLifespans(runtime_paths, lambda: config)

    with capture_logs() as logs:
        await _publish(lifespans, config, runtime_paths)
    await lifespans.stop()

    plugins = config_plugins(config, runtime_paths)
    assert "demo_lifecycle_tool" in plugins.tools
    assert len(plugins.hooks.hooks_for(EVENT_MESSAGE_RECEIVED)) == 1
    assert TestClient(_app(config, runtime_paths)).get("/api/plugins/demo/ping").json() == {"version": 1}
    if error is not None:
        [warning] = [
            log for log in logs if log["event"] == "Plugin lifecycle module failed to load; running no lifespan"
        ]
        assert warning["plugin_name"] == "demo"
        assert error in warning["error"]
    # Without a lifespan there is nothing to start.
    lifespan_errors = [log["event"] for log in logs if log["event"].startswith("Plugin lifespan")]
    assert lifespan_errors == (["Plugin lifespan failed to start"] if error is None else [])


@pytest.mark.parametrize(("lifecycle", "error"), BROKEN_LIFECYCLE_MODULES)
def test_plugin_check_fails_on_a_broken_lifecycle_module(tmp_path: Path, lifecycle: str, error: str) -> None:
    """`mindroom plugins check` refuses a plugin whose lifecycle module cannot load, with the reason."""
    root = _write(tmp_path, _probe(), "demo", lifecycle=lifecycle)

    with pytest.raises(PluginValidationError, match=re.escape(error)):
        check_plugin(root)


def test_the_plugins_other_modules_share_the_lifecycle_module_its_lifespan_runs_in(tmp_path: Path) -> None:
    """Routes and tools that import the lifecycle module see what its lifespan set up."""
    lifecycle = """
from contextlib import asynccontextmanager

STATE = {{}}


@asynccontextmanager
async def lifespan(lifecycle):
    STATE["client"] = "open"
    yield
"""
    routes = """
from fastapi import APIRouter

from .lifecycle import STATE

router = APIRouter()


@router.get("/client")
async def client() -> dict:
    return {{"client": STATE.get("client")}}
"""
    root = _write(
        tmp_path,
        _probe(),
        "demo",
        lifecycle=lifecycle,
        routes=routes,
        tools="from .lifecycle import STATE\n",
    )
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "demo")
    lifespans = PluginLifespans(runtime_paths, lambda: config)

    async def publish_and_stop() -> None:
        await _publish(lifespans, config, runtime_paths)
        await lifespans.stop()

    asyncio.run(publish_and_stop())

    assert TestClient(_app(config, runtime_paths)).get("/api/plugins/demo/client").json() == {"client": "open"}
    lifecycle_module = cached_plugin_module((root / "lifecycle.py").resolve())
    assert cached_plugin_module((root / "tools.py").resolve()).STATE is lifecycle_module.STATE


@asynccontextmanager
async def _serving(app: FastAPI) -> AsyncIterator[str]:
    """Serve ``app`` as the runtime's embedded API server does and yield its websocket base URL."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, ws="websockets-sansio", log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        yield f"ws://127.0.0.1:{port}/api/plugins"
    finally:
        server.should_exit = True
        await serving


async def _closed_with(websocket: aiohttp.ClientWebSocketResponse) -> int:
    """Return the code of the close frame the server sends next."""
    message = await websocket.receive(timeout=5)
    assert message.type == aiohttp.WSMsgType.CLOSE, message
    return message.data


@pytest.mark.asyncio
async def test_a_reload_closes_the_replaced_plugins_websockets_so_clients_reconnect_to_the_new_code(
    tmp_path: Path,
) -> None:
    """An open websocket closes with 1012 once a reload replaces or disables its plugin, and a failed reload keeps it."""
    probe = _probe()
    _write(tmp_path, probe, "demo", routes=ROUTES)
    other_manifest = tmp_path / "plugins" / "other" / "mindroom.plugin.json"
    other_manifest.parent.mkdir(parents=True)
    other_manifest.write_text('{"name": "other"}', encoding="utf-8")
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "demo", "other")
    orchestrator = _MultiAgentOrchestrator(runtime_paths, config=config)
    orchestrator.running = True

    try:
        await orchestrator.reload_plugins_now(source="test")
        async with _serving(_app(config, runtime_paths)) as base, aiohttp.ClientSession() as client:
            websocket = await client.ws_connect(f"{base}/demo/live")
            assert await websocket.receive_str(timeout=5) == "v1"

            # The other plugin claims this one's name, so the reload fails as a whole and publishes nothing.
            other_manifest.write_text('{"name": "demo"}', encoding="utf-8")
            with pytest.raises(PluginValidationError, match="Duplicate plugin manifest names"):
                await orchestrator.reload_plugins_now(source="test")
            await websocket.send_str("still")
            assert await websocket.receive_str(timeout=5) == "v1:still"

            other_manifest.write_text('{"name": "other"}', encoding="utf-8")
            _write(tmp_path, probe, "demo", 2, routes=ROUTES)
            await orchestrator.reload_plugins_now(source="test")
            assert await _closed_with(websocket) == 1012

            websocket = await client.ws_connect(f"{base}/demo/live")
            assert await websocket.receive_str(timeout=5) == "v2"
            await _apply_plugin_change(orchestrator, config, _config(runtime_paths, "other"))
            assert await _closed_with(websocket) == 1012
            async with client.get(f"{base.replace('ws://', 'http://')}/demo/ping") as response:
                assert response.status == 404
    finally:
        await orchestrator.stop()

    # Each reload closes the replaced websocket after the new lifespan started and before the old one stops.
    assert probe.events == [
        ("start", "demo", 1, None, {}),
        ("start", "demo", 2, 1, {}),
        ("closed", 1, 1012),
        ("late send", 1, 1012),
        ("stop", "demo", 1, "reload"),
        ("closed", 2, 1012),
        ("late send", 2, 1012),
        ("stop", "demo", 2, "disabled"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "sent_by_reload"),
    [("stalls", ["websocket.close"]), ("refuses", ["websocket.close"]), ("ended", [])],
    ids=["stalls", "refuses", "ended"],
)
async def test_a_websocket_that_cannot_close_holds_up_no_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: str,
    sent_by_reload: list[str],
) -> None:
    """A replaced plugin's websocket whose close cannot go out is given up on, so the reload completes.

    The server stalls a close while the client stops reading, and refuses one once the client left. A websocket that
    ended before the reload gets no close at all.
    """
    monkeypatch.setattr(plugin_lifecycle, "_LIFESPAN_DEADLINE_SECONDS", 0.5)
    probe = _probe()
    _write(tmp_path, probe, "demo", routes=ROUTES)
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths, "demo")
    lifespans = PluginLifespans(runtime_paths, lambda: config)
    await _publish(lifespans, config, runtime_paths)
    [plugin] = config_plugins(config, runtime_paths).loaded_plugins
    received: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    received.put_nowait({"type": "websocket.connect"})
    sent: list[str] = []
    accepted = asyncio.Event()

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["type"])
        accepted.set()
        if message["type"] == "websocket.close" and server == "stalls":
            # The server waits for the client to read before it writes anything more.
            await asyncio.Event().wait()
        if message["type"] == "websocket.close":
            msg = "Expected ASGI message 'websocket.send' or 'websocket.close', but got 'websocket.close'."
            raise RuntimeError(msg)

    scope = {"type": "websocket", "path": "/live", "root_path": "", "headers": [], "query_string": b""}
    if server == "ended":
        received.put_nowait({"type": "websocket.disconnect", "code": 1000})
    connection = asyncio.create_task(plugin.routes_app(scope, received.get, send))
    try:
        await accepted.wait()
        if server == "ended":
            await connection
        sent.clear()
        async with asyncio.timeout(5):
            await _publish(lifespans, config, runtime_paths)
    finally:
        connection.cancel()
        await asyncio.gather(connection, return_exceptions=True)
        await lifespans.stop()

    assert sent == sent_by_reload
    assert ("stop", "demo", 1, "reload") in probe.events
