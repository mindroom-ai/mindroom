"""WhatsApp tool configuration."""

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
    from agno.tools.whatsapp import WhatsAppTools


@register_tool_with_metadata(
    name="whatsapp",
    display_name="WhatsApp Business",
    description="Send text and template messages via WhatsApp Business API",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiWhatsapp",
    icon_color="text-green-500",
    config_fields=[
        # Authentication/Connection parameters first
        ConfigField(
            name="access_token",
            label="Access Token",
            type="password",
            required=False,
            placeholder="EAAxxxxxxx...",
            description="WhatsApp Business API access token",
        ),
        ConfigField(
            name="phone_number_id",
            label="Phone Number ID",
            type="text",
            required=False,
            placeholder="1234567890123456",
            description="WhatsApp Business Account phone number ID",
        ),
        ConfigField(
            name="version",
            label="API Version",
            type="text",
            required=False,
            default="v22.0",
            placeholder="v22.0",
            description="WhatsApp API version to use",
        ),
        ConfigField(
            name="recipient_waid",
            label="Default Recipient WhatsApp ID",
            type="text",
            required=False,
            default=None,
            placeholder="+1234567890",
            description="Default recipient WhatsApp ID or phone number (optional)",
        ),
        # Feature flags/boolean parameters
        ConfigField(
            name="async_mode",
            label="Async Mode",
            type="boolean",
            required=False,
            default=False,
            description="Enable asynchronous message sending",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/social/whatsapp",
)
def whatsapp_tools() -> type[WhatsAppTools]:
    """Return WhatsApp Business API tools for messaging."""
    from agno.tools.whatsapp import WhatsAppTools

    return WhatsAppTools
