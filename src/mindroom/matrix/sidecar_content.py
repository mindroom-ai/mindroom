"""Canonical Matrix long-text sidecar content parsing."""

from __future__ import annotations

from typing import Any

_LONG_TEXT_METADATA_KEY = "io.mindroom.long_text"


def _validated_mxc_url(value: object) -> str | None:
    """Return one structurally complete Matrix content URI."""
    if not isinstance(value, str) or not value.startswith("mxc://"):
        return None
    server_name, separator, media_id = value[len("mxc://") :].partition("/")
    return value if server_name and separator and media_id else None


def sidecar_mxc_url(content: dict[str, Any]) -> str | None:
    """Return the valid MXC URL for one supported v2 long-text sidecar."""
    metadata = content.get(_LONG_TEXT_METADATA_KEY)
    if not isinstance(metadata, dict) or metadata.get("version") != 2:
        return None
    if metadata.get("encoding") != "matrix_event_content_json":
        return None
    if (url := _validated_mxc_url(content.get("url"))) is not None:
        return url
    encrypted_file = content.get("file")
    if not isinstance(encrypted_file, dict):
        return None
    return _validated_mxc_url(encrypted_file.get("url"))
