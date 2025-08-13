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
    icon="FaPhone",
    icon_color="text-red-600",  # Twilio red
    config_fields=[
        # Authentication - Account credentials
        ConfigField(
            name="account_sid",
            label="Account SID",
            type="text",
            required=False,
            placeholder="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            description="Twilio Account SID (can also be set via TWILIO_ACCOUNT_SID env var)",
        ),
        ConfigField(
            name="auth_token",
            label="Auth Token",
            type="password",
            required=False,
            placeholder="your_auth_token_here",
            description="Twilio Auth Token for basic authentication (can also be set via TWILIO_AUTH_TOKEN env var)",
        ),
        # Alternative API Key authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="SKxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            description="Twilio API Key for key-based authentication (can also be set via TWILIO_API_KEY env var)",
        ),
        ConfigField(
            name="api_secret",
            label="API Secret",
            type="password",
            required=False,
            placeholder="your_api_secret_here",
            description="Twilio API Secret for key-based authentication (can also be set via TWILIO_API_SECRET env var)",
        ),
        # Connection configuration
        ConfigField(
            name="region",
            label="Region",
            type="text",
            required=False,
            placeholder="au1",
            description="Optional Twilio region (e.g. 'au1' for Australia) (can also be set via TWILIO_REGION env var)",
        ),
        ConfigField(
            name="edge",
            label="Edge Location",
            type="text",
            required=False,
            placeholder="sydney",
            description="Optional Twilio edge location (e.g. 'sydney') (can also be set via TWILIO_EDGE env var)",
        ),
        # Feature configuration
        ConfigField(
            name="debug",
            label="Debug Mode",
            type="boolean",
            required=False,
            default=False,
            description="Enable debug logging for Twilio HTTP client",
        ),
    ],
    dependencies=["twilio"],
    docs_url="https://docs.agno.com/tools/toolkits/social/twilio",
)
def twilio_tools() -> type[TwilioTools]:
    """Return Twilio tools for SMS messaging and voice communication."""
    from agno.tools.twilio import TwilioTools

    return TwilioTools
