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
    # LEGACY_COMPAT: Plugin state directories readable by other local users
    # Legacy format: `<storage root>/plugins/<plugin name>/` made by a hook's `ctx.state_root` at the umask's mode.
    # Last legacy release: v2026.11.204 and every release before this change; replacement: unreleased, which makes
    # every plugin state directory at mode 0700.
    # Handling: each access sets the existing directory's mode to 0700, so its files are no longer reachable by others.
    # Coverage: tests/test_plugin_context.py::test_state_root_is_one_private_directory_per_plugin_created_on_first_use
    root.chmod(0o700)
    return root
