"""AWS SES toolkit."""

from __future__ import annotations

from mindroom.tool_system.authoring import Toolkit, tool
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata


class _AWSSESTool(Toolkit):
    """Send plain-text email through Amazon SES."""

    name = "aws_ses_tool"

    def __init__(
        self,
        sender_email: str | None = None,
        sender_name: str | None = None,
        region_name: str = "us-east-1",
        enable_send_email: bool = True,
        all: bool = False,  # noqa: A002 - public config field
    ) -> None:
        self._sender_email = sender_email
        self._sender_name = sender_name
        self._region_name = region_name
        super().__init__()
        if not (all or enable_send_email):
            self.functions.pop("aws_ses_send_email")

    @tool
    def aws_ses_send_email(self, subject: str, body: str, receiver_email: str) -> str:
        """Send plain-text email via Amazon SES.

        Args:
            subject: Non-empty email subject.
            body: Non-empty plain text.
            receiver_email: Recipient email address.

        """
        if not self._sender_email:
            return "Configure the SES sender email first."
        if not (subject.strip() and body.strip() and receiver_email.strip()):
            return "Provide a subject, body, and recipient."
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        source = f"{self._sender_name} <{self._sender_email}>" if self._sender_name else self._sender_email
        try:
            boto3.client("ses", region_name=self._region_name).send_email(
                Source=source,
                Destination={"ToAddresses": [receiver_email]},
                Message={
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
                },
            )
        except (BotoCoreError, ClientError) as exc:
            return f"SES send failed ({type(exc).__name__})."
        return "Email sent."


@register_tool_with_metadata(
    name="aws_ses",
    file_access=ToolFileAccess.NONE,
    display_name="AWS SES",
    description="Send emails using Amazon Simple Email Service",
    category=ToolCategory.EMAIL,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaAws",
    icon_color="text-orange-500",
    config_fields=[
        ConfigField(
            name="sender_email",
            label="Sender Email",
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
            name="region_name",
            label="Region Name",
            type="text",
            required=False,
            default="us-east-1",
        ),
        bool_config_field("enable_send_email", "Enable Send Email"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["boto3"],
    docs_url="https://docs.mindroom.chat/tools/automation-and-platforms/#aws_ses",
    function_names=("aws_ses_send_email",),
)
def aws_ses_tools() -> type[_AWSSESTool]:
    """Return the native aws ses toolkit."""
    return _AWSSESTool
