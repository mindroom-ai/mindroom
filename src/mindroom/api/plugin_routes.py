"""List the published plugins at ``/api/plugins`` and serve each one's HTTP routes under ``/api/plugins/<plugin>/``.

Both follow plugin reloads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Request  # noqa: TC002 - FastAPI resolves the list route's parameter annotation at runtime.
from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.api.client_sessions import presented_session
from mindroom.tool_system.plugin_imports import PLUGIN_API_VERSION
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY, config_plugins

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from mindroom.tool_system.plugins import ConfigPlugins

_MOUNT_PATH = "/api/plugins"
# Uvicorn's websockets-sansio transport frames a websocket's denial response with its own of these and appends the
# app's, and clients reject the duplicates as malformed HTTP. uvicorn 0.52.1 stops duplicating them, and the lock
# overrides openbb-core 1.6's cap below 0.41, but an install of the published package with both the openbb and
# composio extras still resolves uvicorn 0.40. Remove once MindRoom's own dependencies can require 0.52.1.
_DENIAL_FRAMING_HEADERS = frozenset({b"content-length", b"content-type"})


def published_plugins(app: FastAPI) -> ConfigPlugins | None:
    """Return the served config's view of the published plugins, or None before any config has loaded."""
    snapshot = config_lifecycle.require_api_state(app).snapshot
    if snapshot.runtime_config is None:
        return None
    return config_plugins(snapshot.runtime_config, snapshot.runtime_paths)


class _PluginRoutesApp:
    """Forward each request to the routes app of the plugin its first path segment names, among those published now.

    Each plugin's routes app is built as the plugin loads, and a reload publishes new ones, so the plugin is looked up
    on every request rather than mounted once.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer one request with its plugin's routes, or 404 when no published plugin of that name has routes."""
        # Starlette's Mount keeps the full path in ``path`` and moves the matched prefix into ``root_path``.
        root_path = scope.get("root_path", "")
        name = scope["path"].removeprefix(root_path).removeprefix("/").partition("/")[0]
        routes_app = self._routes_app(name)
        if routes_app is None:
            # A websocket gets the same 404 as its handshake's denial response.
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        plugin_root = f"{root_path}/{name}"
        # Without the main app's ``router`` the plugin's own takes its place, and ``app_root_path`` is where
        # ``request.url_for`` and ``request.base_url`` start, so URLs a plugin generates reach its own routes.
        sub_scope = {key: value for key, value in scope.items() if key != "router"}
        await routes_app(
            {**sub_scope, "root_path": plugin_root, "app_root_path": plugin_root, PLUGIN_NAME_SCOPE_KEY: name},
            receive,
            send,
        )

    def _routes_app(self, name: str) -> ASGIApp | None:
        plugins = published_plugins(self._app)
        if plugins is None:
            return None
        return next((plugin.routes_app for plugin in plugins.loaded_plugins if plugin.name == name), None)


class FramedWebsocketDenials:
    """Leave each websocket denial's framing headers to the server, so clients can read a refused handshake."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass ``scope`` to the app, dropping the app's framing headers from a websocket's denial response."""
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return

        async def framed(message: Message) -> None:
            if message["type"] == "websocket.http.response.start":
                headers = [
                    (key, value)
                    for key, value in message.get("headers", ())
                    if key.lower() not in _DENIAL_FRAMING_HEADERS
                ]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, framed)


async def _list_plugins(request: Request) -> dict[str, object]:
    """Answer a signed-in client with this MindRoom's plugin API version and each published plugin's name.

    Each plugin also names the plugin API version its manifest needs; nothing else about it is listed, no path, setting,
    or load error.
    """
    presented_session(request)
    plugins = published_plugins(request.app)
    major, minor = PLUGIN_API_VERSION
    return {
        "plugin_api_version": f"{major}.{minor}",
        "plugins": [
            {"name": plugin.name, "mindroom_plugin_api": plugin.mindroom_plugin_api}
            for plugin in (plugins.loaded_plugins if plugins is not None else ())
        ],
    }


def mount_plugin_routes(app: FastAPI) -> None:
    """Serve the plugin list and the published plugins' routes on ``app``; add them before any catch-all route."""
    app.add_api_route(_MOUNT_PATH, _list_plugins, methods=["GET"])
    app.mount(_MOUNT_PATH, _PluginRoutesApp(app))
