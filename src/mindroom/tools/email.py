"""Email tool configuration."""

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
    from agno.tools.email import EmailTools


@register_tool_with_metadata(
    name="email",
    display_name="Email",
    description="Send emails via SMTP (Gmail)",
    category=ToolCategory.EMAIL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Mail",
    icon_color="text-blue-600",
    config_fields=[
        # Authentication/Connection parameters
        ConfigField(
            name="receiver_email",
            label="Receiver Email",
            type="text",
            required=False,
            default=None,
            placeholder="recipient@example.com",
            description="Default recipient email address",
        ),
        ConfigField(
            name="sender_name",
            label="Sender Name",
            type="text",
            required=False,
            default=None,
            placeholder="Your Name",
            description="Name to display as the sender",
        ),
        ConfigField(
            name="sender_email",
            label="Sender Email",
            type="text",
            required=False,
            default=None,
            placeholder="your.email@gmail.com",
            description="Gmail address to send emails from",
        ),
        ConfigField(
            name="sender_passkey",
            label="Sender Password/App Password",
            type="password",
            required=False,
            default=None,
            placeholder="Gmail password or app-specific password",
            description="Gmail password or app-specific password for authentication",
        ),
    ],
    dependencies=[],  # Uses built-in smtplib
    docs_url="https://docs.agno.com/tools/toolkits/social/email",
)
def email_tools() -> type[EmailTools]:
    """Return email tools for sending messages via SMTP."""
    from agno.tools.email import EmailTools

    return EmailTools
