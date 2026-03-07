"""Matrix homeserver health helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

_MATRIX_VERSIONS_PATH = "/_matrix/client/versions"


def matrix_versions_url(homeserver_url: str) -> str:
    """Return the Matrix versions endpoint for a homeserver URL."""
    return f"{homeserver_url.rstrip('/')}{_MATRIX_VERSIONS_PATH}"


def response_has_matrix_versions(response: httpx.Response) -> bool:
    """Return whether a response is a successful Matrix `/versions` payload."""
    if not response.is_success:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and "versions" in payload
