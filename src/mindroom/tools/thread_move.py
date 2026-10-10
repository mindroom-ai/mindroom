"""Thread move tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.thread_move import ThreadMoveTools


@register_tool_with_metadata(
    name="thread_move",
    file_access=ToolFileAccess.NONE,
    display_name="Thread Move",
    description="Move a conversation thread into another room",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Share2",
    icon_color="text-sky-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=("move_thread",),
    requires_room_context=True,
)
def thread_move_tools() -> type[ThreadMoveTools]:
    """Return the Matrix thread move tool."""
    from mindroom.custom_tools.thread_move import ThreadMoveTools

    return ThreadMoveTools
