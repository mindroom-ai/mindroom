"""Canonical Matrix long-text sidecar content parsing."""

from __future__ import annotations

from typing import Any

_LONG_TEXT_METADATA_KEY = "io.mindroom.long_text"


def sidecar_mxc_url(content: dict[str, Any]) -> str | None:
    """Return the valid MXC URL for one supported v2 long-text sidecar."""
    metadata = content.get(_LONG_TEXT_METADATA_KEY)
    if not isinstance(metadata, dict) or metadata.get("version") != 2:
        return None
    if metadata.get("encoding") != "matrix_event_content_json":
        return None
    url = content.get("url")
    if isinstance(url, str) and url.startswith("mxc://"):
        return url
    encrypted_file = content.get("file")
    if not isinstance(encrypted_file, dict):
        return None
    encrypted_url = encrypted_file.get("url")
    if isinstance(encrypted_url, str) and encrypted_url.startswith("mxc://"):
        return encrypted_url
    return None
