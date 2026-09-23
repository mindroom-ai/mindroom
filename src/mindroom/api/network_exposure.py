"""Network exposure policy for the bundled dashboard API.

With no dashboard credential configured, `authenticate_user` treats every
request as the standalone administrator. That open-access mode is only
defensible while the dashboard is reached from this machine under a host name
the operator controls, so this module owns:

- whether a runtime serves the dashboard without any credential,
- which `Host` values such a dashboard answers, which is what stops a
  DNS-rebound attacker host from becoming the dashboard's own origin,
- the ASGI guard that applies that allow-list before routing,
- the startup warning for an unauthenticated bind.

The guard is installed by the entry points that actually expose the app over
the network (the embedded orchestrator server and `python -m mindroom.api.main`),
because the allow-list only makes sense for a served socket.
"""

from __future__ import annotations

import ipaddress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_DASHBOARD_ALLOWED_HOSTS_ENV = "MINDROOM_DASHBOARD_ALLOWED_HOSTS"

# Liveness and readiness probes address the runtime by the scheduler's own
# routable address (a pod IP, a bridge IP), so the allow-list skips them. A
# probe is not a browser and never marks its own provenance, which keeps the
# exemption out of reach of a rebound page.
_HOST_GUARD_EXEMPT_PATHS = frozenset({"/api/health", "/api/ready"})
_BROWSER_PROVENANCE_HEADERS = frozenset({b"origin", b"sec-fetch-site", b"sec-fetch-mode"})


def _env_text(runtime_paths: RuntimePaths, name: str) -> str | None:
    value = runtime_paths.env_value(name)
    if value is None:
        return None
    return value.strip() or None


def _dashboard_open_access(runtime_paths: RuntimePaths) -> bool:
    """Return whether this runtime authenticates dashboard requests without a credential."""
    if _env_text(runtime_paths, "MINDROOM_API_KEY") is not None:
        return False
    supabase_configured = (
        _env_text(runtime_paths, "SUPABASE_URL") is not None
        and _env_text(runtime_paths, "SUPABASE_ANON_KEY") is not None
    )
    if supabase_configured:
        return False
    return not runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED")


def _is_loopback_host(hostname: str) -> bool:
    """Return whether one host name always addresses this machine."""
    candidate = _normalized_host(hostname)
    if not candidate:
        return False
    if candidate == "localhost" or candidate.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def is_loopback_origin(origin: str) -> bool:
    """Return whether one browser origin was served from this machine."""
    parsed = urlsplit(origin.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        return False
    return _is_loopback_host(parsed.hostname)


def _allowed_dashboard_hosts(runtime_paths: RuntimePaths) -> frozenset[str]:
    """Return the configured non-loopback host names an open dashboard answers."""
    hosts: set[str] = set()
    public_url = _env_text(runtime_paths, "MINDROOM_PUBLIC_URL")
    public_host = urlsplit(public_url).hostname if public_url else None
    if public_host:
        hosts.add(public_host.lower())
    configured = _env_text(runtime_paths, _DASHBOARD_ALLOWED_HOSTS_ENV)
    if configured:
        hosts.update(_normalized_host(entry) for entry in configured.split(",") if entry.strip())
    hosts.discard("")
    return frozenset(hosts)


def _dashboard_host_allowed(host_header: str | None, runtime_paths: RuntimePaths) -> bool:
    """Return whether one request `Host` addresses this dashboard by an expected name."""
    hostname = _host_header_name(host_header)
    if hostname is None:
        return False
    allowed = _allowed_dashboard_hosts(runtime_paths)
    return "*" in allowed or hostname in allowed or _is_loopback_host(hostname)


def warn_unauthenticated_dashboard_exposure(runtime_paths: RuntimePaths, *, host: str) -> None:
    """Log how an unauthenticated dashboard is exposed before it serves requests."""
    if not _dashboard_open_access(runtime_paths):
        return
    if _is_loopback_host(host):
        logger.warning(
            "dashboard_unauthenticated",
            bind_host=host,
            detail=(
                "No MINDROOM_API_KEY is set, so every dashboard request is served as the administrator. "
                "Set MINDROOM_API_KEY to require a credential."
            ),
        )
        return
    logger.warning(
        "dashboard_unauthenticated_non_loopback_bind",
        bind_host=host,
        detail=(
            "No MINDROOM_API_KEY is set while the dashboard binds a non-loopback interface, so every "
            "request from that network is served as the administrator. Set MINDROOM_API_KEY, or bind "
            "127.0.0.1. Requests are answered only for loopback host names, MINDROOM_PUBLIC_URL, and "
            f"{_DASHBOARD_ALLOWED_HOSTS_ENV}."
        ),
    )


class DashboardHostGuard:
    """Reject unexpected `Host` values before an open-access dashboard routes them."""

    def __init__(self, app: ASGIApp, runtime_paths: RuntimePaths) -> None:
        self.app = app
        self.runtime_paths = runtime_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass an expected request through, and refuse any other `Host`."""
        if scope["type"] not in {"http", "websocket"} or self._host_allowed(scope):
            await self.app(scope, receive, send)
            return
        logger.warning(
            "dashboard_host_rejected",
            host=_scope_host_header(scope),
            path=scope.get("path", ""),
        )
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        response = PlainTextResponse(
            "Invalid host header. An unauthenticated MindRoom dashboard answers only loopback host "
            f"names, MINDROOM_PUBLIC_URL, and {_DASHBOARD_ALLOWED_HOSTS_ENV}.",
            status_code=400,
        )
        await response(scope, receive, send)

    def _host_allowed(self, scope: Scope) -> bool:
        if _is_infrastructure_probe(scope):
            return True
        if not _dashboard_open_access(self.runtime_paths):
            return True
        return _dashboard_host_allowed(_scope_host_header(scope), self.runtime_paths)


def _is_infrastructure_probe(scope: Scope) -> bool:
    """Return whether one request is a liveness probe rather than a browser request."""
    if scope.get("path", "") not in _HOST_GUARD_EXEMPT_PATHS:
        return False
    return not any(key in _BROWSER_PROVENANCE_HEADERS for key, _ in scope.get("headers", ()))


def _normalized_host(value: str) -> str:
    return value.strip().strip("[]").rstrip(".").lower()


def _host_header_name(host_header: str | None) -> str | None:
    """Return the host name of one `Host` header value, without its port."""
    if host_header is None:
        return None
    value = host_header.strip().lower()
    if not value:
        return None
    if value.startswith("["):
        closing = value.find("]")
        return _normalized_host(value[1:closing]) if closing > 1 else None
    name = value.rsplit(":", 1)[0] if ":" in value else value
    return _normalized_host(name) or None


def _scope_host_header(scope: Scope) -> str | None:
    """Return the one `Host` header value, or nothing when it is absent or ambiguous."""
    values = [value.decode("latin-1") for key, value in scope.get("headers", ()) if key == b"host"]
    return values[0] if len(values) == 1 else None
