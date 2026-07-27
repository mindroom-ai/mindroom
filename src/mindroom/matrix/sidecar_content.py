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


def _sidecar_metadata(content: dict[str, Any]) -> dict[str, Any] | None:
    """Return the metadata block of one supported v2 long-text sidecar."""
    metadata = content.get(_LONG_TEXT_METADATA_KEY)
    if not isinstance(metadata, dict) or metadata.get("version") != 2:
        return None
    if metadata.get("encoding") != "matrix_event_content_json":
        return None
    return metadata


def sidecar_mxc_url(content: dict[str, Any]) -> str | None:
    """Return the valid MXC URL for one supported v2 long-text sidecar."""
    if _sidecar_metadata(content) is None:
        return None
    if (url := _validated_mxc_url(content.get("url"))) is not None:
        return url
    encrypted_file = content.get("file")
    if not isinstance(encrypted_file, dict):
        return None
    return _validated_mxc_url(encrypted_file.get("url"))


def sidecar_declared_bytes(content: dict[str, Any]) -> int:
    """Return the resolved size one long-text sidecar says it will hydrate to.

    The stub that replaces an oversized body is small, so a byte-bounded read that priced only the
    stored payload would admit thousands of them and then hydrate every one. The writer records the
    pre-offload size in ``original_event_size``, which lets the bound charge what hydration will
    actually cost without joining the plaintext table or downloading anything.

    That value is already UTF-8 bytes, not characters - ``_calculate_event_size`` encodes the
    canonical JSON before measuring it, and adds a 2 KB allowance for event metadata. It therefore
    slightly overstates a stub's true cost, which is the safe direction for a budget.

    The value is only read for content that is already a structurally valid sidecar, and a stub
    whose declaration is missing or nonsensical is charged nothing beyond its stored payload -
    under-charging degrades to today's behaviour, while trusting a negative number would let a stub
    pay for its neighbours.
    """
    if _sidecar_metadata(content) is None or sidecar_mxc_url(content) is None:
        return 0
    metadata = content[_LONG_TEXT_METADATA_KEY]
    declared_bytes = metadata.get("original_event_size")
    if not isinstance(declared_bytes, int) or isinstance(declared_bytes, bool) or declared_bytes < 0:
        return 0
    return declared_bytes
