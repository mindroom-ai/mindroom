"""AWS Lambda tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.aws_lambda import AWSLambdaTools


@register_tool_with_metadata(
    name="aws_lambda",
    display_name="AWS Lambda",
    description="Serverless function management and execution",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaAws",
    icon_color="text-orange-500",
    config_fields=[
        # Configuration
        ConfigField(
            name="region_name",
            label="Region Name",
            type="text",
            required=False,
            default="us-east-1",
            placeholder="us-east-1",
            description="AWS region name where Lambda functions are located",
        ),
    ],
    dependencies=["boto3"],
    docs_url="https://docs.agno.com/tools/toolkits/others/aws_lambda",
)
def aws_lambda_tools() -> type[AWSLambdaTools]:
    """Return AWS Lambda tools for serverless function management."""
    from agno.tools.aws_lambda import AWSLambdaTools

    return AWSLambdaTools
