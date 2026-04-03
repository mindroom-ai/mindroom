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
        ConfigField(
            name="enable_send_text_message",
            label="Enable Send Text Message",
            type="boolean",
            required=False,
            default=True,
            description="Enable plain text WhatsApp messages",
        ),
        ConfigField(
            name="enable_send_template_message",
            label="Enable Send Template Message",
            type="boolean",
            required=False,
            default=True,
            description="Enable WhatsApp template messages",
        ),
        ConfigField(
            name="enable_send_reply_buttons",
            label="Enable Send Reply Buttons",
            type="boolean",
            required=False,
            default=False,
            description="Enable interactive reply button messages",
        ),
        ConfigField(
            name="enable_send_list_message",
            label="Enable Send List Message",
            type="boolean",
            required=False,
            default=False,
            description="Enable interactive list messages",
        ),
        ConfigField(
            name="enable_send_image",
            label="Enable Send Image",
            type="boolean",
            required=False,
            default=False,
            description="Enable image messages",
        ),
        ConfigField(
            name="enable_send_document",
            label="Enable Send Document",
            type="boolean",
            required=False,
            default=False,
            description="Enable document messages",
        ),
        ConfigField(
            name="enable_send_location",
            label="Enable Send Location",
            type="boolean",
            required=False,
            default=False,
            description="Enable location messages",
        ),
        ConfigField(
            name="enable_send_reaction",
            label="Enable Send Reaction",
            type="boolean",
            required=False,
            default=False,
            description="Enable reaction messages",
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
            description="Enable all WhatsApp tools",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.agno.com/tools/toolkits/social/whatsapp",
)
def whatsapp_tools() -> type[WhatsAppTools]:
    """Return WhatsApp Business API tools for messaging."""
    from agno.tools.whatsapp import WhatsAppTools

    return WhatsAppTools
