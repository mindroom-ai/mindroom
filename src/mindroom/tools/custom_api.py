"""Custom API tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.api import CustomApiTools


@register_tool_with_metadata(
    name="custom_api",
    display_name="Custom API",
    description="Make HTTP requests to any external API with customizable authentication and parameters",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Globe",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="username",
            label="Username",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="password",
            label="Password",
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
            name="headers",
            label="Headers",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="verify_ssl",
            label="Verify Ssl",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
        ConfigField(
            name="enable_make_request",
            label="Enable Make Request",
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
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/others/custom_api",
)
def custom_api_tools() -> type[CustomApiTools]:
    """Return Custom API tools for making HTTP requests to external APIs."""
    from agno.tools.api import CustomApiTools

    return CustomApiTools
