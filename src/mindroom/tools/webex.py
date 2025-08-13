"""Webex tool configuration."""

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
    from agno.tools.webex import WebexTools


@register_tool_with_metadata(
    name="webex",
    display_name="Webex",
    description="Video conferencing and messaging platform for teams",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaVideo",
    icon_color="text-blue-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="access_token",
            label="Access Token",
            type="password",
            required=False,
            placeholder="Bearer token from Webex Developer Portal",
            description="Webex access token for authentication (can also be set via WEBEX_ACCESS_TOKEN env var)",
        ),
        # Feature flags
        ConfigField(
            name="send_message",
            label="Send Message",
            type="boolean",
            required=False,
            default=True,
            description="Enable sending messages to Webex spaces/rooms",
        ),
        ConfigField(
            name="list_rooms",
            label="List Rooms",
            type="boolean",
            required=False,
            default=True,
            description="Enable listing Webex spaces/rooms",
        ),
    ],
    dependencies=["webexpythonsdk"],
    docs_url="https://docs.agno.com/tools/toolkits/social/webex",
)
def webex_tools() -> type[WebexTools]:
    """Return Webex tools for video conferencing and messaging."""
    from agno.tools.webex import WebexTools

    return WebexTools
