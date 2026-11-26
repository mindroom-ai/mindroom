"""Resend email toolkit."""

from __future__ import annotations

from os import getenv

from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata


class ResendTools(Toolkit):
    """Send transactional email through Resend."""

    name = "resend_tools"

    def __init__(
        self,
        api_key: str | None = None,
        from_email: str | None = None,
        enable_send_email: bool = True,
        all: bool = False,  # noqa: A002 - public config field
    ) -> None:
        self._api_key = api_key or getenv("RESEND_API_KEY")
        self._from_email = from_email
        super().__init__()
        if not (all or enable_send_email):
            self.functions.pop("send_email")

    @tool
    def send_email(self, to_email: str, subject: str, body: str) -> str:
        """Send an HTML email via Resend.

        Args:
            to_email: Recipient email address.
            subject: Email subject.
            body: HTML message body.

        """
        if not self._api_key or not self._from_email:
            return "Configure the Resend API key and sender first."
        if not to_email.strip():
            return "Provide a recipient email address."
        from resend import RequestsClient

        # Emails.send reads a process-global API key. Keep credentials scoped to this request.
        try:
            _, status, _ = RequestsClient().request(
                "POST",
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self._api_key}", "User-Agent": "MindRoom"},
                json={"from": self._from_email, "to": to_email, "subject": subject, "html": body},
            )
        except RuntimeError:
            return "Resend request failed."
        return "Email sent." if 200 <= status < 300 else f"Resend rejected the email (HTTP {status})."


@register_tool_with_metadata(
    name="resend",
    file_access=ToolFileAccess.NONE,
    display_name="Resend",
    description="Email delivery service for sending transactional emails",
    category=ToolCategory.EMAIL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Mail",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("RESEND_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="from_email",
            label="From Email",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_send_email",
            label="Enable Send Email",
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
    dependencies=["resend"],
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#resend",
    function_names=("send_email",),
)
def resend_tools() -> type[ResendTools]:
    """Return the native resend toolkit."""
    return ResendTools
