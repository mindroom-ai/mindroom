"""Browser origin normalization and strict allowlist validation."""

from __future__ import annotations

import ipaddress
import json
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_DEFAULT_PORTS = {"http": 80, "https": 443}


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


def parse_browser_origin(value: str) -> str:
    """Validate one explicit HTTPS or loopback HTTP origin and return it normalized."""
    origin = normalized_origin(value)
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    loopback = host == "localhost"
    if not loopback:
        with suppress(ValueError):
            loopback = ipaddress.ip_address(host).is_loopback
    if (
        origin is None
        or parsed.path not in {"", "/"}
        or not (origin.startswith("https://") or loopback)
        or any(c in "*?#" or c.isspace() or ord(c) < 32 for c in value)
    ):
        msg = f"Browser origin must be explicit HTTPS or loopback HTTP: {value!r}"
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
