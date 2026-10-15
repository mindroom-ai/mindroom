"""Stop reply tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.stop_reply import StopReplyTools


@register_tool_with_metadata(
    name="stop_reply",
    file_access=ToolFileAccess.NONE,
    display_name="Stop Reply",
    description="Stop the reply another agent or team is writing in the current conversation",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_room_context=True,
    icon="MessageSquare",
    icon_color="text-red-500",
    dependencies=["agno"],
    docs_url="https://github.com/mindroom-ai/mindroom",
    function_names=("stop_agent_reply",),
)
def stop_reply_tools() -> type[StopReplyTools]:
    """Return the tools that stop another agent's reply."""
    from mindroom.custom_tools.stop_reply import StopReplyTools

    return StopReplyTools
