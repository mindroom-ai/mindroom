"""Small shared transport for operational CLI reads."""

from __future__ import annotations

import math
from ipaddress import ip_address
from typing import TYPE_CHECKING

from mindroom.constants import DEFAULT_MINDROOM_URL

if TYPE_CHECKING:
    import httpx

    from mindroom.constants import RuntimePaths


def _is_loopback(host: str) -> bool:
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


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
    if token and parsed.scheme == "http" and not _is_loopback(parsed.host):
        msg = "Use HTTPS when sending MINDROOM_API_KEY to a remote endpoint."
        raise ValueError(msg)
    try:
        return httpx.get(
            f"{base_url.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        msg = "Cannot reach MindRoom; check --url / MINDROOM_URL and that its API is running."
        raise ValueError(msg) from exc
