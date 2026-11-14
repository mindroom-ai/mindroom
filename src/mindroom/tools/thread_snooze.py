"""Thread snooze tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.thread_snooze import ThreadSnoozeTools


@register_tool_with_metadata(
    name="thread_snooze",
    file_access=ToolFileAccess.NONE,
    display_name="Thread Snooze",
    description="Snooze a thread until a set time and bring it back with a message",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="AlarmClock",
    icon_color="text-amber-500",
    docs_url="https://docs.mindroom.chat/tools/matrix-and-attachments/#thread_snooze",
    function_names=("snooze_thread", "unsnooze_thread"),
    requires_room_context=True,
)
def thread_snooze_tools() -> type[ThreadSnoozeTools]:
    """Return thread snooze tools."""
    from mindroom.custom_tools.thread_snooze import ThreadSnoozeTools

    return ThreadSnoozeTools
