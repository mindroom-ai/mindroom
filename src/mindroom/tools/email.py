"""Email toolkit."""

from __future__ import annotations

import smtplib
import ssl
from email.headerregistry import Address
from email.message import EmailMessage

from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata


class _EmailTools(Toolkit):
    """Send email to a configured recipient via Gmail SMTP."""

    name = "email_tools"

    def __init__(
        self,
        receiver_email: str | None = None,
        sender_name: str | None = None,
        sender_email: str | None = None,
        sender_passkey: str | None = None,
        enable_email_user: bool = True,
        all: bool = False,  # noqa: A002 - public config field
    ) -> None:
        self._receiver = receiver_email
        self._name = sender_name
        self._sender = sender_email
        self._passkey = sender_passkey
        super().__init__()
        if not (all or enable_email_user):
            self.functions.pop("smtp_send_email")

    @tool
    def smtp_send_email(self, subject: str, body: str) -> str:
        """Send plain-text email to the configured recipient via Gmail SMTP."""
        if not (self._receiver and self._name and self._sender and self._passkey):
            return "Configure the SMTP sender, passkey, and recipient first."
        message = EmailMessage()
        try:
            message["Subject"] = subject
            message["From"] = Address(display_name=self._name, addr_spec=self._sender)
            message["To"] = self._receiver
            message.set_content(body)
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30, context=ssl.create_default_context()) as smtp:
                smtp.login(self._sender, self._passkey)
                smtp.send_message(message)
        except (OSError, ValueError) as exc:
            return f"SMTP send failed ({type(exc).__name__})."
        return "Email sent."


@register_tool_with_metadata(
    name="email",
    file_access=ToolFileAccess.NONE,
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
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#email",
    function_names=("smtp_send_email",),
)
def email_tools() -> type[_EmailTools]:
    """Return the native email toolkit."""
    return _EmailTools
