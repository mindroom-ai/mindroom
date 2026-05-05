"""Canonical plugin identity validation helpers."""

from __future__ import annotations

import re

_PLUGIN_NAME_PATTERN = re.compile(r"^[a-z0-9_-]+$")


def validate_plugin_name(plugin_name: str) -> str:
    """Validate one plugin identifier and return the normalized value."""
    normalized = plugin_name.strip()
    if not normalized or not _PLUGIN_NAME_PATTERN.fullmatch(normalized):
        msg = (
            f"Invalid plugin name: {plugin_name!r}. "
            "Plugin names must use lowercase ASCII letters, digits, hyphens, or underscores."
        )
        raise ValueError(msg)
    return normalized
