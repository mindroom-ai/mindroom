"""Thread tags tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolManagedInitArg, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.thread_tags import ThreadTagsTools


@register_tool_with_metadata(
    name="thread_tags",
    file_access=ToolFileAccess.NONE,
    display_name="Thread Tags",
    description="Add, remove, and view tags on conversation threads",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Tags",
    icon_color="text-emerald-500",
    docs_url="https://docs.mindroom.chat/tools/matrix-and-attachments/#thread_tags",
    function_names=("list_thread_tags", "tag_thread", "untag_thread"),
    managed_init_args=(
        ToolManagedInitArg.RUNTIME_PATHS,
        ToolManagedInitArg.CURRENT_ROOM_ID,
    ),
    requires_room_context=True,
)
def thread_tags_tools() -> type[ThreadTagsTools]:
    """Return Matrix thread tagging tools."""
    from mindroom.custom_tools.thread_tags import ThreadTagsTools

    return ThreadTagsTools
