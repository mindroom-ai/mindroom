"""Workspace memory write tool metadata registration."""

from mindroom.tools_metadata import (
    TOOL_METADATA,
    SetupType,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
)

TOOL_METADATA["write_memory"] = ToolMetadata(
    name="write_memory",
    display_name="Write Workspace Memory",
    description="Write explicit notes to daily logs or MEMORY.md workspace files",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="NotebookPen",
    icon_color="text-emerald-500",
    config_fields=[],
    dependencies=[],
)
