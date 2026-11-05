"""Serve each published plugin's HTTP routes under ``/api/plugins/<plugin>/``, following plugin reloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.tool_system.plugins import config_plugins

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.types import ASGIApp, Receive, Scope, Send

_MOUNT_PATH = "/api/plugins"


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
        await routes_app({**sub_scope, "root_path": plugin_root, "app_root_path": plugin_root}, receive, send)

    def _routes_app(self, name: str) -> ASGIApp | None:
        snapshot = config_lifecycle.require_api_state(self._app).snapshot
        if snapshot.runtime_config is None:
            return None
        plugins = config_plugins(snapshot.runtime_config, snapshot.runtime_paths).loaded_plugins
        return next((plugin.routes_app for plugin in plugins if plugin.name == name), None)


def mount_plugin_routes(app: FastAPI) -> None:
    """Serve the published plugins' routes on ``app``; mount before any catch-all route that would shadow them."""
    app.mount(_MOUNT_PATH, _PluginRoutesApp(app))
