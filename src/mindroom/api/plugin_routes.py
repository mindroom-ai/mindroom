"""Serve each published plugin's HTTP routes under ``/api/plugins/<plugin>/``, following plugin reloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from mindroom.api import config_lifecycle
from mindroom.tool_system.plugins import config_plugins

if TYPE_CHECKING:
    from fastapi import APIRouter
    from starlette.types import Receive, Scope, Send

_MOUNT_PATH = "/api/plugins"


class _PluginRoutesApp:
    """Forward each request to the router of the plugin its first path segment names, among those published now.

    A reload publishes new router objects, so the plugin is looked up on every request rather than mounted once.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        # Keyed by plugin name and rebuilt when its router object changes: a key of id(router) could hand a new router
        # the stale sub-app of a freed one whose id it reused, and would keep every replaced router alive.
        self._sub_apps: dict[str, tuple[APIRouter, FastAPI]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer one request with its plugin's router, or 404 when no published plugin of that name has routes."""
        # Starlette's Mount keeps the full path in ``path`` and moves the matched prefix into ``root_path``.
        root_path = scope.get("root_path", "")
        name = scope["path"].removeprefix(root_path).removeprefix("/").partition("/")[0]
        router = self._published_router(name) if name else None
        if router is None:
            # A websocket gets the same 404 as its handshake's denial response.
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        plugin_root = f"{root_path}/{name}"
        # Without the main app's ``router`` the sub-app's own takes its place, and ``app_root_path`` is where
        # ``request.url_for`` and ``request.base_url`` start, so URLs a plugin generates reach its own routes.
        sub_scope = {key: value for key, value in scope.items() if key != "router"}
        await self._sub_app(name, router)(
            {**sub_scope, "root_path": plugin_root, "app_root_path": plugin_root},
            receive,
            send,
        )

    def _published_router(self, name: str) -> APIRouter | None:
        snapshot = config_lifecycle.require_api_state(self._app).snapshot
        if snapshot.runtime_config is None:
            return None
        for plugin in config_plugins(snapshot.runtime_config, snapshot.runtime_paths).loaded_plugins:
            if plugin.name == name:
                return plugin.router
        return None

    def _sub_app(self, name: str, router: APIRouter) -> FastAPI:
        cached = self._sub_apps.get(name)
        if cached is not None and cached[0] is router:
            return cached[1]
        sub_app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
        # Errors are answered as the main API answers them, so a rejected body is not echoed back.
        sub_app.exception_handlers.update(self._app.exception_handlers)
        sub_app.include_router(router)
        # Inside a plugin route ``request.app`` is this sub-app; its state is the main app's, so ``ensure_app_state``
        # finds the client sessions and room membership that ``mindroom.plugin_http`` reads.
        sub_app.state = self._app.state
        self._sub_apps[name] = (router, sub_app)
        return sub_app


def mount_plugin_routes(app: FastAPI) -> None:
    """Serve the published plugins' routes on ``app``; mount before any catch-all route that would shadow them."""
    app.mount(_MOUNT_PATH, _PluginRoutesApp(app))
