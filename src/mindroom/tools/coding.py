"""Coding tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from mindroom.custom_tools.coding import CodingTools


@register_tool_with_metadata(
    name="coding",
    display_name="Coding Tools",
    description="Advanced code-oriented file operations (precise edits, grep, and discovery). Prefer this over file for coding agents; keep file for backward compatibility.",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Code",
    icon_color="text-purple-500",
    config_fields=[
        ConfigField(
            name="base_dir",
            label="Base Directory",
            type="text",
            required=False,
            default=None,
            description="Working directory for file operations. Defaults to current directory.",
        ),
    ],
    dependencies=[],
)
def coding_tools() -> type[CodingTools]:
    """Return ergonomic coding tools for LLM agents."""
    from mindroom.custom_tools.coding import CodingTools

    return CodingTools
