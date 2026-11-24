"""React-only tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.react import ReactTools


@register_tool_with_metadata(
    name="react",
    file_access=ToolFileAccess.NONE,
    display_name="React",
    description=(
        "Add an emoji reaction to a message in the current room. "
        "It cannot send, edit, or read messages, so it suits agents that may only acknowledge, such as listening agents."
    ),
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_room_context=True,
    icon="SmilePlus",
    icon_color="text-amber-500",
    docs_url="https://docs.mindroom.chat/tools/matrix-and-attachments/#react",
    function_names=("react",),
)
def react_tools() -> type[ReactTools]:
    """Return the reaction-only toolkit."""
    from mindroom.custom_tools.react import ReactTools

    return ReactTools
