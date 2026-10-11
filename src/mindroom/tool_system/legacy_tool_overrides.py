"""Directed errors for retired authored tool override fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# LEGACY_COMPAT: Retired per-tool restrict_to_base_dir override.
# Legacy format: file, coding, and python tool entries accepted an authored boolean restrict_to_base_dir.
# Last legacy release: v2026.9.277 (latest tag, still shipping the ConfigField); replacement: the unreleased
# agents.<name>.file_access and defaults.file_access settings removed the field.
# Handling: Reject it on every tool with guidance to the agent or defaults file_access setting.
# Coverage: tests/test_tools_metadata.py::test_restrict_to_base_dir_is_rejected_with_file_access_hint.
_RETIRED_TOOL_OVERRIDE_GUIDANCE = {
    "restrict_to_base_dir": "use agents.<name>.file_access or defaults.file_access",
}


def retired_tool_override(overrides: Mapping[str, object]) -> tuple[str, str] | None:
    """Return the first retired override field and its replacement guidance, if the entry authors one."""
    for field_name, guidance in _RETIRED_TOOL_OVERRIDE_GUIDANCE.items():
        if field_name in overrides:
            return field_name, guidance
    return None
