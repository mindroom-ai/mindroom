"""AWS SES tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.aws_ses import AWSSESTool


@register_tool_with_metadata(
    name="aws_ses",
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
    dependencies=["boto3"],
    docs_url="https://docs.agno.com/tools/toolkits/others/aws_ses",
)
def aws_ses_tools() -> type[AWSSESTool]:
    """Return AWS SES tools for sending emails."""
    from agno.tools.aws_ses import AWSSESTool

    return AWSSESTool
