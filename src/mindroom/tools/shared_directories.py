"""Shared directories tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.shared_directories import SharedDirectoryTools


@register_tool_with_metadata(
    name="shared_directories",
    file_access=ToolFileAccess.NONE,
    display_name="Shared Directories",
    description="Create directories that other agents can read from their own workers",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_primary_runtime=True,
    icon="FolderOpen",
    icon_color="text-amber-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=(
        "create_shared_directory",
        "invite_to_shared_directory",
        "remove_from_shared_directory",
        "delete_shared_directory",
        "list_shared_directories",
    ),
)
def shared_directories_tools() -> type[SharedDirectoryTools]:
    """Return the tools that share directories between agents."""
    from mindroom.custom_tools.shared_directories import SharedDirectoryTools

    return SharedDirectoryTools
