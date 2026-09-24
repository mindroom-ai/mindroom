"""Coding tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolExecutionTarget,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.coding import CodingTools


@register_tool_with_metadata(
    name="coding",
    display_name="Coding Tools",
    description="Find code, search across files, and make precise edits",
    category=ToolCategory.DEVELOPMENT,
    file_access=ToolFileAccess.AGENT,
    managed_init_args=(ToolManagedInitArg.FILE_ACCESS,),
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.WORKER,
    consumes_workspace_paths=True,
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
            authored_override=False,
        ),
    ],
    dependencies=[],
    function_names=("edit_file", "find_files", "grep", "ls", "read_file", "write_file"),
)
def coding_tools() -> type[CodingTools]:
    """Return ergonomic coding tools for LLM agents."""
    from mindroom.custom_tools.coding import CodingTools

    return CodingTools
