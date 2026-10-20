"""Browser origin normalization and strict allowlist validation."""

from __future__ import annotations

import ipaddress
import json
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mindroom.runtime_env_policy import CONNECTIONS_ALLOWED_ORIGINS_ENV

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_DEFAULT_PORTS = {"http": 80, "https": 443}
# The private networks MindRoom Chat also accepts over plain HTTP: RFC 1918 and IPv6 unique-local addresses.
_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)
_HOSTED_CHAT_ORIGIN = "https://chat.mindroom.chat"


def normalized_origin(url: str | None) -> str | None:
    """Return the origin a browser would send for one URL, or None when it has none."""
    try:
        parsed = urlsplit((url or "").strip())
        scheme, host, port = parsed.scheme, parsed.hostname, parsed.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS or not host or parsed.netloc.endswith(":"):
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if ":" in host:
        try:
            host = f"[{ipaddress.ip_address(host.split('%')[0])}]"
        except ValueError:
            return None
    elif not host.isascii():
        # Python's idna codec is IDNA2003, browsers use UTS 46; require the explicit xn-- form.
        return None
    return f"{scheme}://{host}" + ("" if port in {None, _DEFAULT_PORTS[scheme]} else f":{port}")


def _is_local_host(host: str) -> bool:
    """Return whether a URL host is `localhost` or a loopback or private-network IP literal, where plain HTTP is accepted.

    DNS names other than `localhost` need HTTPS even when they resolve to a private address, as in MindRoom Chat.
    """
    if host == "localhost":
        return True
    with suppress(ValueError):
        address = ipaddress.ip_address(host)
        return address.is_loopback or any(address in network for network in _PRIVATE_NETWORKS)
    return False


def is_local_origin(origin: str | None) -> bool:
    """Return whether `origin` is a normalized browser origin on a local address."""
    return (
        origin is not None and normalized_origin(origin) == origin and _is_local_host(urlsplit(origin).hostname or "")
    )


def local_api_origin(paths: RuntimePaths) -> str:
    """Return MindRoom's own API origin on this machine, which links, callbacks, and token audiences use by default."""
    return f"http://localhost:{paths.env_value('MINDROOM_PORT', default='8765') or '8765'}"


def deployment_origin(paths: RuntimePaths, request_url: str) -> str | None:
    """Return the browser origin of this deployment: `MINDROOM_PUBLIC_URL` when set, else the request's own URL."""
    return normalized_origin(paths.env_value("MINDROOM_PUBLIC_URL") or request_url)


def parse_browser_origin(value: str) -> str:
    """Validate one explicit HTTPS origin, or HTTP origin on a local address, and return it normalized."""
    origin = normalized_origin(value)
    parsed = urlsplit(value)
    if (
        origin is None
        or parsed.path not in {"", "/"}
        or not (origin.startswith("https://") or _is_local_host(parsed.hostname or ""))
        or any(c in "*?#" or c.isspace() or ord(c) < 32 for c in value)
    ):
        msg = f"Browser origin must be explicit HTTPS, or HTTP on a local address: {value!r}"
        raise ValueError(msg)
    return origin


def allowed_client_origins(paths: RuntimePaths, env_name: str) -> tuple[str, ...]:
    """Read a JSON list of exact web and bundled iOS app origins from `env_name`; invalid configuration fails closed."""
    try:
        origins = json.loads(paths.env_value(env_name, default="[]") or "[]")
        if not isinstance(origins, list) or not all(isinstance(origin, str) for origin in origins):
            return ()
        # The bundled iOS app has a fixed custom-scheme origin. Accept only
        # this literal, never arbitrary capacitor origins or opaque "null".
        return tuple(
            origin if origin == "capacitor://localhost" else parse_browser_origin(origin) for origin in origins
        )
    except ValueError:
        return ()


def connections_client_origin_allowed(paths: RuntimePaths, client_origin: str, server_origin: str | None) -> bool:
    """Return whether MindRoom Chat at `client_origin` may sign users in to the Connections portal at `server_origin`.

    Without a configured list, MindRoom's hosted Chat may, and so may any Chat on a local address when the portal
    is on one too, as in a Docker stack on a home network.
    """
    if (paths.env_value(CONNECTIONS_ALLOWED_ORIGINS_ENV) or "").strip():
        return client_origin in allowed_client_origins(paths, CONNECTIONS_ALLOWED_ORIGINS_ENV)
    return client_origin == _HOSTED_CHAT_ORIGIN or (is_local_origin(server_origin) and is_local_origin(client_origin))


def connections_sign_in_enabled(paths: RuntimePaths) -> bool:
    """Return whether some MindRoom Chat may sign users in: the list is unset, or lists at least one valid origin."""
    configured = (paths.env_value(CONNECTIONS_ALLOWED_ORIGINS_ENV) or "").strip()
    return not configured or bool(allowed_client_origins(paths, CONNECTIONS_ALLOWED_ORIGINS_ENV))
