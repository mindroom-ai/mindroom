"""Resend email tool configuration."""

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
    from agno.tools.resend import ResendTools


@register_tool_with_metadata(
    name="resend",
    display_name="Resend",
    description="Email delivery service for sending transactional emails",
    category=ToolCategory.EMAIL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Mail",
    icon_color="text-blue-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="re_...",
            description="Resend API key for authentication (can also be set via RESEND_API_KEY env var)",
        ),
        # Configuration
        ConfigField(
            name="from_email",
            label="From Email",
            type="text",
            required=False,
            placeholder="noreply@example.com",
            description="Default sender email address for outgoing emails",
        ),
    ],
    dependencies=["resend"],
    docs_url="https://docs.agno.com/tools/toolkits/others/resend",
)
def resend_tools() -> type[ResendTools]:
    """Return Resend email tools for sending transactional emails."""
    from agno.tools.resend import ResendTools

    return ResendTools
