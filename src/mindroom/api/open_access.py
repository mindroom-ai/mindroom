"""Browser guards for requests the API serves without a credential.

Such a request is authorized only by who can reach the server, so it must name
one of this runtime's own hosts, which defeats DNS rebinding, and must not come
from another site's page. Dashboard authentication applies these guards when no
dashboard credential is configured, and `/v1` applies them when it is
unauthenticated. Both read the allow-list from the request's current runtime.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from starlette.datastructures import Headers

    from mindroom.constants import RuntimePaths

_ALLOWED_HOSTS_ENV = "MINDROOM_DASHBOARD_ALLOWED_HOSTS"
# The runtime is told it is reached at these URLs, so their hosts are its own.
_SELF_URL_ENVS = ("MINDROOM_PUBLIC_URL", "MINDROOM_BASE_URL", "MINDROOM_URL", "MINDROOM_SCRIPT_GATEWAY_URL")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_REMEDY = f"to {_ALLOWED_HOSTS_ENV} or configure authentication"
_AUTHORITY = re.compile(r"(\[[0-9a-f:.]+\]|[a-z0-9._-]+)(?::[0-9]*)?")


@dataclass(frozen=True)
class _OpenAccessRejection:
    """Why one request without a credential is refused."""

    status_code: int
    detail: str


def open_access_rejection(
    headers: Headers,
    method: str,
    runtime_paths: RuntimePaths,
) -> _OpenAccessRejection | None:
    """Return why a request served without a credential must be refused, or None when it may proceed."""
    configured = _configured_hosts(runtime_paths)
    host_header = headers.get("host", "")
    host = _authority_host(host_header)
    # DNS rebinding needs a name the attacker's DNS answers, so a page names an address only when served from it.
    if host is None or not (host in configured or _is_local(host) or _address(host) is not None):
        return _OpenAccessRejection(400, f"Host '{host_header}' is not allowed without a credential; add it {_REMEDY}")
    origin = headers.get("origin")
    if origin is not None:
        origin_host = _url_host(origin)
        if origin_host is None or not (origin_host == host or origin_host in configured or _is_local(origin_host)):
            return _OpenAccessRejection(
                403,
                f"Origin '{origin}' is not allowed without a credential; add its host {_REMEDY}",
            )
    if method not in _SAFE_METHODS and headers.get("sec-fetch-site") == "cross-site":
        return _OpenAccessRejection(403, "Cross-site browser requests are not allowed without a credential")
    return None


def _configured_hosts(runtime_paths: RuntimePaths) -> set[str]:
    hosts = {_url_host(runtime_paths.env_value(name) or "") for name in _SELF_URL_ENVS}
    hosts.update(_authority_host(entry) for entry in (runtime_paths.env_value(_ALLOWED_HOSTS_ENV) or "").split(","))
    return {host for host in hosts if host}


def _authority_host(authority: str) -> str | None:
    match = _AUTHORITY.fullmatch(authority.strip().lower())
    if match is None:
        return None
    return match.group(1).strip("[]").rstrip(".") or None


def _url_host(url: str) -> str | None:
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return None
    return host.rstrip(".") if host else None


def _is_local(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    address = _address(host)
    return address is not None and address.is_loopback


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None
