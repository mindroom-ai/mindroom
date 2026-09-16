"""MindRoom Chat UI action tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.chat_ui import ChatUITools


@register_tool_with_metadata(
    name="chat_ui",
    display_name="Chat UI",
    description="Let an agent request bounded actions in MindRoom Chat",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_room_context=True,
    icon="PanelsTopLeft",
    icon_color="text-violet-500",
    dependencies=["agno"],
    docs_url="https://docs.mindroom.chat/tools/chat-ui/",
    function_names=("show_computer", "open_settings", "open_panel"),
)
def chat_ui_tools() -> type[ChatUITools]:
    """Return bounded MindRoom Chat UI action tools."""
    from mindroom.custom_tools.chat_ui import ChatUITools

    return ChatUITools
