"""OAuth connection management tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolManagedInitArg, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.oauth_connections import OAuthConnectionTools


@register_tool_with_metadata(
    name="oauth_connections",
    display_name="OAuth Connections",
    description="Reset the current requester's OAuth connections for the current agent",
    category=ToolCategory.INTEGRATIONS,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Unplug",
    icon_color="text-amber-500",
    config_fields=[],
    dependencies=["agno"],
    managed_init_args=(ToolManagedInitArg.RUNTIME_PATHS,),
    function_names=("reset_oauth_connection",),
)
def oauth_connections_tools() -> type[OAuthConnectionTools]:
    """Return narrow OAuth connection management tools."""
    from mindroom.custom_tools.oauth_connections import OAuthConnectionTools

    return OAuthConnectionTools
