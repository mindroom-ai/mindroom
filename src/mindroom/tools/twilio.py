"""Twilio tool configuration."""

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
    from agno.tools.twilio import TwilioTools


@register_tool_with_metadata(
    name="twilio",
    display_name="Twilio",
    description="SMS messaging and voice communication platform",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiTwilio",
    icon_color="text-red-600",  # Twilio red
    config_fields=[
        ConfigField(
            name="account_sid",
            label="Account Sid",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="auth_token",
            label="Auth Token",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="api_secret",
            label="API Secret",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="region",
            label="Region",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="edge",
            label="Edge",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="debug",
            label="Debug",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_send_sms",
            label="Enable Send Sms",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_call_details",
            label="Enable Get Call Details",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_list_messages",
            label="Enable List Messages",
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
    ],
    dependencies=["twilio"],
    docs_url="https://docs.agno.com/tools/toolkits/social/twilio",
)
def twilio_tools() -> type[TwilioTools]:
    """Return Twilio tools for SMS messaging and voice communication."""
    from agno.tools.twilio import TwilioTools

    return TwilioTools
