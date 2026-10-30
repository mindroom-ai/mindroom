"""Browser origin normalization and strict allowlist validation."""

from __future__ import annotations

import ipaddress
import json
from contextlib import suppress
from dataclasses import replace
from types import MappingProxyType
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
_IPV4_DIGITS = {
    8: frozenset("01234567"),
    10: frozenset("0123456789"),
    16: frozenset("0123456789abcdefABCDEF"),
}


def _parse_ipv4_number(piece: str) -> int | None:
    """Parse one WHATWG IPv4 number piece: decimal, 0-prefixed octal, or 0x-prefixed hex."""
    radix = 10
    digits = piece
    if piece[:2].lower() == "0x":
        radix, digits = 16, piece[2:]
    elif len(piece) > 1 and piece.startswith("0"):
        radix, digits = 8, piece[1:]
    if not digits:
        return 0 if radix != 10 else None
    if any(character not in _IPV4_DIGITS[radix] for character in digits):
        return None
    return int(digits, radix)


def _parse_ipv4(host: str) -> str | None:
    """Return the canonical dotted quad for a WHATWG IPv4 host spelling, or None when it is not one."""
    pieces = host.split(".")
    if pieces[-1] == "":
        pieces.pop()
    if not pieces or len(pieces) > 4:
        return None
    values: list[int] = []
    for piece in pieces:
        number = _parse_ipv4_number(piece)
        if number is None:
            return None
        values.append(number)
    if any(number > 255 for number in values[:-1]) or values[-1] >= 256 ** (5 - len(values)):
        return None
    address = values[-1] + sum(number * 256 ** (3 - index) for index, number in enumerate(values[:-1]))
    return str(ipaddress.IPv4Address(address))


def _ends_in_a_number(host: str) -> bool:
    """Return whether the host's last piece is numeric, which makes browsers parse the host as an IPv4 address."""
    pieces = host.split(".")
    piece = pieces[-1]
    if not piece and len(pieces) > 1:
        piece = pieces[-2]
    if not piece:
        return False
    return (piece.isascii() and piece.isdigit()) or _parse_ipv4_number(piece) is not None


def _canonical_host(host: str) -> str | None:
    """Return the host as a browser writes it in an origin, or None when it has no canonical form."""
    if ":" in host:
        try:
            return f"[{ipaddress.ip_address(host.split('%', maxsplit=1)[0])}]"
        except ValueError:
            return None
    if _ends_in_a_number(host):
        # Browsers parse a host ending in a number as an IPv4 address (127.1, 0x7f.1, 2130706433)
        # and reject it when the pieces do not form one; canonicalize or fail the same way.
        return _parse_ipv4(host)
    if not host.isascii():
        # Python's idna codec is IDNA2003, browsers use UTS 46; require the explicit xn-- form.
        return None
    return host


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
    canonical = _canonical_host(host)
    if canonical is None:
        return None
    return f"{scheme}://{canonical}" + ("" if port in {None, _DEFAULT_PORTS[scheme]} else f":{port}")


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


def with_api_port(paths: RuntimePaths, api_port: int) -> RuntimePaths:
    """Return paths whose `MINDROOM_PORT` is the port the API serves on, unless the operator set one."""
    if (paths.env_value("MINDROOM_PORT") or "").strip():
        return paths
    return replace(paths, process_env=MappingProxyType({**paths.process_env, "MINDROOM_PORT": str(api_port)}))


def deployment_origin(public_url: str | None, fallback_url: str) -> str | None:
    """Return the browser origin of this deployment: the configured `MINDROOM_PUBLIC_URL` when set, else the fallback's."""
    return normalized_origin((public_url or "").strip() or fallback_url)


def parse_browser_origin(value: str) -> str:
    """Validate one explicit HTTPS origin, or HTTP origin on a local address, and return it normalized."""
    origin = normalized_origin(value)
    parsed = urlsplit(value)
    host = urlsplit(origin).hostname if origin is not None else ""
    if (
        origin is None
        or parsed.path not in {"", "/"}
        or not (origin.startswith("https://") or _is_local_host(host or ""))
        or any(c in "*?#" or c.isspace() or ord(c) < 32 for c in value)
    ):
        msg = f"Browser origin must be explicit HTTPS, or HTTP on a local address: {value!r}"
        raise ValueError(msg)
    return origin


def dashboard_origin(paths: RuntimePaths) -> str | None:
    """Return the origin `MINDROOM_DASHBOARD_URL` names, where the dashboard is served when not at the public origin."""
    value = (paths.env_value("MINDROOM_DASHBOARD_URL") or "").strip()
    return parse_browser_origin(value) if value else None


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


def connections_origins_configured(paths: RuntimePaths) -> bool:
    """Return whether the operator lists Connections Chat origins, which replaces the defaults."""
    return bool((paths.env_value(CONNECTIONS_ALLOWED_ORIGINS_ENV) or "").strip())


def connections_client_origin_allowed(paths: RuntimePaths, client_origin: str, server_origin: str | None) -> bool:
    """Return whether MindRoom Chat at `client_origin` may sign users in to the Connections portal at `server_origin`.

    Without a configured list, MindRoom's hosted Chat may, and so may any Chat on a local address when the portal
    is on one too, as in a Docker stack on a home network.
    """
    if connections_origins_configured(paths):
        return client_origin in allowed_client_origins(paths, CONNECTIONS_ALLOWED_ORIGINS_ENV)
    return client_origin == _HOSTED_CHAT_ORIGIN or (is_local_origin(server_origin) and is_local_origin(client_origin))


def connections_sign_in_enabled(paths: RuntimePaths) -> bool:
    """Return whether some MindRoom Chat may sign users in: the list is unset, or lists at least one valid origin."""
    return not connections_origins_configured(paths) or bool(
        allowed_client_origins(paths, CONNECTIONS_ALLOWED_ORIGINS_ENV),
    )
