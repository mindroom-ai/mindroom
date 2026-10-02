"""Small shared transport for operational CLI reads."""

from __future__ import annotations

import math
from dataclasses import dataclass
from ipaddress import ip_address
from typing import TYPE_CHECKING

from mindroom.constants import DEFAULT_MINDROOM_URL

if TYPE_CHECKING:
    import httpx

    from mindroom.constants import RuntimePaths


def is_loopback_host(host: str) -> bool:
    """Return whether a host name or address only reaches this machine."""
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@dataclass(frozen=True)
class ApiTarget:
    """A checked MindRoom API base URL with the headers and proxy setting its requests must use."""

    base_url: str
    headers: dict[str, str]
    trust_env: bool


def resolve_api_target(runtime_paths: RuntimePaths, url: str | None, *, require_key: bool = False) -> ApiTarget:
    """Pick the MindRoom URL, refusing to send an operator key over remote HTTP or through an environment proxy."""
    import httpx  # noqa: PLC0415

    base_url = url or runtime_paths.env_value("MINDROOM_URL") or DEFAULT_MINDROOM_URL
    try:
        parsed = httpx.URL(base_url)
    except httpx.InvalidURL as exc:
        msg = "Invalid MindRoom URL."
        raise ValueError(msg) from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host or parsed.userinfo or parsed.query or parsed.fragment:
        msg = "Use an absolute HTTP(S) URL without credentials, query, or fragment."
        raise ValueError(msg)
    token = runtime_paths.env_value("MINDROOM_API_KEY")
    if require_key and not token:
        msg = "MINDROOM_API_KEY is required for this operational check."
        raise ValueError(msg)
    if token and parsed.scheme == "http" and not is_loopback_host(parsed.host):
        msg = "Use HTTPS when sending MINDROOM_API_KEY to a remote endpoint."
        raise ValueError(msg)
    return ApiTarget(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {token}"} if token else {},
        trust_env=not (token and parsed.scheme == "http"),
    )


def get_api_response(
    runtime_paths: RuntimePaths,
    url: str | None,
    path: str,
    timeout: float,
    *,
    require_key: bool = False,
) -> httpx.Response:
    """Read one endpoint without redirecting or sending an operator key over remote HTTP."""
    import httpx  # noqa: PLC0415

    if not math.isfinite(timeout):
        msg = "--timeout must be finite."
        raise ValueError(msg)
    target = resolve_api_target(runtime_paths, url, require_key=require_key)
    try:
        return httpx.get(
            f"{target.base_url}{path}",
            headers=target.headers,
            timeout=timeout,
            follow_redirects=False,
            trust_env=target.trust_env,
        )
    except httpx.TimeoutException as exc:
        msg = "MindRoom API request timed out."
        raise TimeoutError(msg) from exc
    except httpx.HTTPError as exc:
        msg = "Cannot reach MindRoom; check --url / MINDROOM_URL and that its API is running."
        raise ValueError(msg) from exc
