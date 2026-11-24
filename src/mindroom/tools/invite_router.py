"""Registration of the retired `invite_router` tool name."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.invite_router import InviteRouterTools


@register_tool_with_metadata(
    name="invite_router",
    file_access=ToolFileAccess.NONE,
    display_name="Invite Router",
    description="Deprecated: no longer needed, rooms work without another account",
    docs_url="https://docs.mindroom.chat/authorization/",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_primary_runtime=True,
    function_names=("invite_router",),
)
def invite_router_tools() -> type[InviteRouterTools]:
    """Return the retired router invite stub."""
    from mindroom.custom_tools.invite_router import InviteRouterTools

    return InviteRouterTools
