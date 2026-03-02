"""Webex tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
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
    icon="SiWebex",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="enable_send_message",
            label="Enable Send Message",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_list_rooms",
            label="Enable List Rooms",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="access_token",
            label="Access Token",
            type="password",
            required=False,
            default=None,
        ),
    ],
    dependencies=["webexpythonsdk"],
    docs_url="https://docs.agno.com/tools/toolkits/social/webex",
)
def webex_tools() -> type[WebexTools]:
    """Return Webex tools for video conferencing and messaging."""
    from agno.tools.webex import WebexTools

    return WebexTools
