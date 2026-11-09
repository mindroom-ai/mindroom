"""Serve each plugin's origin router on hosts of its own, on a listener of its own beside the API.

With ``MINDROOM_PLUGIN_ORIGIN_PORT`` set, the listener forwards each request for ``<label>.<plugin>.<domain>``, where
``<domain>`` is ``MINDROOM_PLUGIN_ORIGIN_DOMAIN``, to the router of that plugin's ``origin_module``, with the API app as
``request.app`` and the plugin's name in scope, as the API forwards ``/api/plugins/<plugin>/`` to its routes.
A page on such a host shares no origin with the dashboard or with any other plugin's hosts.

On any other host, ``GET /ask?domain=<host>`` answers Caddy's on-demand TLS check: 200 while the plugin's
``serves(label)`` says it serves that host, else 404.
Every answer says that only its own origin and the chat's origins may frame it, beside whatever the route says.
"""

from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.datastructures import Headers, QueryParams
from starlette.responses import JSONResponse, Response

from mindroom.api import config_lifecycle
from mindroom.api.plugin_routes import framed_by_server, published_plugins
from mindroom.api.request_body_limit import RequestBodyLimitMiddleware
from mindroom.api.script_gateway import listener_port, serve_listener
from mindroom.logging_config import get_logger
from mindroom.origins import allowed_client_origins
from mindroom.runtime_env_policy import COMPUTER_ALLOWED_ORIGINS_ENV
from mindroom.tool_system.plugin_imports import DNS_LABEL
from mindroom.tool_system.plugins import PLUGIN_NAME_SCOPE_KEY

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from fastapi import FastAPI
    from starlette.types import Message, Receive, Scope, Send

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.plugins import PluginRoutesApp

__all__ = ["plugin_origin_url", "serve_plugin_origins"]

logger = get_logger(__name__)

_PORT_ENV = "MINDROOM_PLUGIN_ORIGIN_PORT"
_DOMAIN_ENV = "MINDROOM_PLUGIN_ORIGIN_DOMAIN"
_PUBLIC_PORT_ENV = "MINDROOM_PLUGIN_ORIGIN_PUBLIC_PORT"
_ASK_PATH = "/ask"
_SERVES_TIMEOUT_SECONDS = 5


def _domain(paths: RuntimePaths) -> str:
    return (paths.env_value(_DOMAIN_ENV) or "").strip().lower().rstrip(".") or "localhost"


def plugin_origin_url(paths: RuntimePaths, plugin_name: str, label: str) -> str | None:
    """Return the public URL of ``plugin_name``'s host ``label``, or None while plugin origins are off.

    It raises ``ValueError`` when ``label`` is not one DNS label.
    """
    if not DNS_LABEL.fullmatch(label):
        msg = f"A plugin origin's label is one DNS label of lowercase letters, digits, and hyphens, not {label!r}."
        raise ValueError(msg)
    port = listener_port(paths, _PORT_ENV)
    if port is None:
        return None
    domain = _domain(paths)
    local = domain == "localhost" or domain.endswith(".localhost")
    # Browsers reach a local listener directly, so its own port is the one in its URLs.
    public_port = listener_port(paths, _PUBLIC_PORT_ENV) or (port if local else None)
    authority = f"{label}.{plugin_name}.{domain}" + (f":{public_port}" if public_port else "")
    return f"{'http' if local else 'https'}://{authority}"


def _plugin_host(host: str, domain: str) -> tuple[str, str] | None:
    """Return the label and plugin name of ``host``, with or without a port, when it is a plugin host under ``domain``."""
    try:
        hostname = (urlsplit(f"//{host}").hostname or "").rstrip(".")
    except ValueError:
        return None
    label, _, plugin_name = hostname.removesuffix(f".{domain}").partition(".")
    if not hostname.endswith(f".{domain}") or not (DNS_LABEL.fullmatch(label) and DNS_LABEL.fullmatch(plugin_name)):
        return None
    return label, plugin_name


class _PluginOrigins:
    """The listener's app: each plugin host's requests go to its plugin's origin router, among those published now."""

    def __init__(self, api_app: FastAPI) -> None:
        self._api_app = api_app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer one request or websocket on a plugin host with its plugin's router, or as the ask endpoint."""
        paths = config_lifecycle.require_api_state(self._api_app).snapshot.runtime_paths
        send = _with_frame_floor(send, allowed_client_origins(paths, COMPUTER_ALLOWED_ORIGINS_ENV))
        if scope["type"] == "websocket":
            send = framed_by_server(send)
        host = _plugin_host(Headers(scope=scope).get("host", ""), _domain(paths))
        not_found = JSONResponse({"detail": "Not Found"}, status_code=404)
        if host is not None:
            _, plugin_name = host
            origin = self._origin(plugin_name)
            if origin is not None:
                origin_app, _ = origin
                # Bodies are capped as on the API, since FastAPI reads a body before a route's gate runs.
                await RequestBodyLimitMiddleware(origin_app)(
                    {**scope, "app": self._api_app, PLUGIN_NAME_SCOPE_KEY: plugin_name},
                    receive,
                    send,
                )
                return
            # A websocket gets the same 404 as its handshake's denial response.
            response: Response = not_found
        elif scope["type"] == "http" and scope["method"] == "GET" and scope["path"] == _ASK_PATH:
            response = await self._ask(QueryParams(scope["query_string"]).get("domain", ""), _domain(paths))
        else:
            response = not_found
        await response(scope, receive, send)

    def _origin(self, name: str) -> tuple[PluginRoutesApp, Callable[[str], object] | None] | None:
        """Return the origin app and ``serves`` of the published plugin ``name``, or None when it serves no origins."""
        plugins = published_plugins(self._api_app)
        for plugin in plugins.loaded_plugins if plugins is not None else ():
            if plugin.name == name and plugin.origin_app is not None:
                return plugin.origin_app, plugin.origin_serves
        return None

    async def _ask(self, domain: str, origin_domain: str) -> Response:
        """Answer 200 when ``domain`` is a host whose plugin's ``serves`` says yes within the deadline, else 404."""
        host = _plugin_host(domain, origin_domain)
        origin = self._origin(host[1]) if host is not None else None
        served = False
        if host is not None and origin is not None and (serves := origin[1]) is not None:
            label, plugin_name = host
            try:
                async with asyncio.timeout(_SERVES_TIMEOUT_SECONDS):
                    answer = serves(label)
                    served = (await answer if inspect.isawaitable(answer) else answer) is True
            except Exception:
                logger.exception("Plugin origin serves() failed", plugin_name=plugin_name)
        return Response(status_code=200 if served else 404, headers={"cache-control": "no-store"})


def _with_frame_floor(send: Send, chat_origins: tuple[str, ...]) -> Send:
    """Add who may frame each answer: its own origin and the chat's, as a policy of its own beside the route's."""
    floor = (b"content-security-policy", " ".join(("frame-ancestors 'self'", *chat_origins)).encode())

    async def framed(message: Message) -> None:
        if message["type"] in {"http.response.start", "websocket.http.response.start"}:
            message = {**message, "headers": [*message.get("headers", ()), floor]}
        await send(message)

    return framed


@asynccontextmanager
async def serve_plugin_origins(
    runtime_paths: RuntimePaths,
    *,
    host: str,
    api_app: FastAPI,
    log_level: str,
) -> AsyncIterator[None]:
    """Serve the plugins' origins on ``MINDROOM_PLUGIN_ORIGIN_PORT`` while the primary API runs; nothing while unset."""
    port = listener_port(runtime_paths, _PORT_ENV)
    if port is None:
        yield
        return
    # An invalid public port or domain fails the start, rather than each link a route makes.
    listener_port(runtime_paths, _PUBLIC_PORT_ENV)
    if not all(DNS_LABEL.fullmatch(part) for part in _domain(runtime_paths).split(".")):
        msg = f"{_DOMAIN_ENV} must be a domain name, such as example-apps.net, not {_domain(runtime_paths)!r}."
        raise ValueError(msg)
    async with serve_listener(
        _PluginOrigins(api_app),
        name="plugin_origins_listener",
        host=host,
        port=port,
        log_level=log_level,
        ws="websockets-sansio",
        # A path or query may carry a one-use link.
        access_log=False,
    ):
        yield
