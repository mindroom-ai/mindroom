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
    description=(
        "Open MindRoom Chat UI for the user: show the agent's worker browser in the Computer panel "
        "with open_panel(panel='computer'), open Settings, or show room members. "
        "Sends a UI request; does not navigate or control the user's local browser."
    ),
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_primary_runtime=True,
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
