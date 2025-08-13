"""Zoom tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.zoom import ZoomTools


@register_tool_with_metadata(
    name="zoom",
    display_name="Zoom",
    description="Video conferencing platform for scheduling and managing meetings",
    category=ToolCategory.SOCIAL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.OAUTH,
    icon="FaVideo",
    icon_color="text-blue-500",  # Zoom blue
    config_fields=[
        # Authentication parameters
        ConfigField(
            name="account_id",
            label="Account ID",
            type="text",
            required=False,
            placeholder="your_account_id",
            description="Zoom account ID from Server-to-Server OAuth app (can also be set via ZOOM_ACCOUNT_ID env var)",
        ),
        ConfigField(
            name="client_id",
            label="Client ID",
            type="text",
            required=False,
            placeholder="your_client_id",
            description="Client ID from Server-to-Server OAuth app (can also be set via ZOOM_CLIENT_ID env var)",
        ),
        ConfigField(
            name="client_secret",
            label="Client Secret",
            type="password",
            required=False,
            placeholder="your_client_secret",
            description="Client secret from Server-to-Server OAuth app (can also be set via ZOOM_CLIENT_SECRET env var)",
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/social/zoom",
)
def zoom_tools() -> type[ZoomTools]:
    """Return Zoom tools for video conferencing and meeting management."""
    from agno.tools.zoom import ZoomTools

    return ZoomTools
