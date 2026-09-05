"""Router invite recovery tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.invite_router import InviteRouterTools


@register_tool_with_metadata(
    name="invite_router",
    display_name="Invite Router",
    description="Invite the MindRoom router to the current Matrix room",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_room_context=True,
    dependencies=["agno"],
    function_names=("invite_router",),
)
def invite_router_tools() -> type[InviteRouterTools]:
    """Return router invite recovery tools."""
    from mindroom.custom_tools.invite_router import InviteRouterTools

    return InviteRouterTools
