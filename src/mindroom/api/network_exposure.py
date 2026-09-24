"""Host allow-list for a dashboard that serves requests without a credential.

Without a dashboard credential every request is served as the administrator,
so a DNS-rebound attacker host must never become the dashboard's own origin.
The embedded API server wraps the app with `guard_unauthenticated_dashboard`.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

from mindroom.api.auth import dashboard_open_access, is_loopback_host
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

# The URLs the runtime is told it is reached at name hosts it answers to.
_SELF_URL_ENVS = ("MINDROOM_PUBLIC_URL", "MINDROOM_SCRIPT_GATEWAY_URL", "MINDROOM_URL")
_EXTRA_HOSTS_ENV = "MINDROOM_DASHBOARD_ALLOWED_HOSTS"
_HOST_HEADER = re.compile(r"(\[[0-9a-f:.]+\]|[a-z0-9._-]+)(:[0-9]+)?")


def guard_unauthenticated_dashboard(app: ASGIApp, runtime_paths: RuntimePaths, *, host: str) -> ASGIApp:
    """Return the app to serve on `host`, behind a Host allow-list when it needs no credential."""
    if not dashboard_open_access(runtime_paths):
        return app
    logger.warning(
        "dashboard_unauthenticated",
        bind_host=host,
        detail="No MINDROOM_API_KEY is set, so every dashboard request is served as the administrator.",
    )
    allowed = _allowed_hosts(runtime_paths)

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and not _host_allowed(Headers(scope=scope).get("host"), allowed):
            response = PlainTextResponse(f"Invalid host header; name this host in {_EXTRA_HOSTS_ENV}", 400)
            await response(scope, receive, send)
            return
        await app(scope, receive, send)

    return guarded


def _allowed_hosts(runtime_paths: RuntimePaths) -> frozenset[str]:
    urls = [runtime_paths.env_value(name) or "" for name in _SELF_URL_ENVS]
    extra = (runtime_paths.env_value(_EXTRA_HOSTS_ENV) or "").split(",")
    hosts = {urlsplit(url).hostname or "" for url in urls} | {entry.strip().lower() for entry in extra}
    return frozenset(hosts - {""})


def _host_allowed(host_header: str | None, allowed: frozenset[str]) -> bool:
    match = _HOST_HEADER.fullmatch((host_header or "").strip().lower())
    if match is None:
        return False
    name = match.group(1).strip("[]").rstrip(".")
    return name in allowed or is_loopback_host(name) or _is_address(name)


def _is_address(name: str) -> bool:
    # DNS rebinding needs a name the attacker's DNS answers for; a page is only
    # same-origin with an address literal when it is served from that address.
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True
