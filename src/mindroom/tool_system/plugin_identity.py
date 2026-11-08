"""Canonical plugin identity helpers: name validation and each plugin's state directory."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

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


def plugin_state_root(storage_root: Path, plugin_name: str) -> Path:
    """Return the plugin's own state directory under ``storage_root``, created at mode 0700 on first use."""
    root = storage_root / "plugins" / validate_plugin_name(plugin_name)
    root.mkdir(parents=True, exist_ok=True)
    # chmod rather than mkdir's mode, so a directory that already exists, such as one an earlier release made for a
    # hook at the umask's mode, becomes private too.
    root.chmod(0o700)
    return root
