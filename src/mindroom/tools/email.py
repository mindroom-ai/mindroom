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
        ConfigField(
            name="receiver_email",
            label="Receiver Email",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="sender_name",
            label="Sender Name",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="sender_email",
            label="Sender Email",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="sender_passkey",
            label="Sender Passkey",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_email_user",
            label="Enable Email User",
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
    dependencies=[],  # Uses built-in smtplib
    docs_url="https://docs.agno.com/tools/toolkits/social/email",
)
def email_tools() -> type[EmailTools]:
    """Return email tools for sending messages via SMTP."""
    from agno.tools.email import EmailTools

    return EmailTools
