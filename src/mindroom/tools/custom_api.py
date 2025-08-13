"""Custom API tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

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
        # Connection parameters
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default=None,
            placeholder="https://api.example.com",
            description="Base URL for API calls. If not provided, full URLs must be specified in endpoints",
        ),
        # Authentication parameters
        ConfigField(
            name="username",
            label="Username",
            type="text",
            required=False,
            default=None,
            placeholder="username",
            description="Username for basic authentication",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            default=None,
            placeholder="password",
            description="Password for basic authentication",
        ),
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
            placeholder="sk-...",
            description="API key for bearer token authentication",
        ),
        # Headers configuration
        ConfigField(
            name="headers",
            label="Default Headers",
            type="text",
            required=False,
            default=None,
            placeholder='{"Content-Type": "application/json"}',
            description="Default headers to include in requests (JSON format)",
        ),
        # Configuration parameters
        ConfigField(
            name="verify_ssl",
            label="Verify SSL",
            type="boolean",
            required=False,
            default=True,
            description="Whether to verify SSL certificates for HTTPS requests",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
            placeholder="30",
            description="Request timeout in seconds",
        ),
        # Feature flags
        ConfigField(
            name="make_request",
            label="Enable Make Request",
            type="boolean",
            required=False,
            default=True,
            description="Whether to register the make_request function",
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.agno.com/tools/toolkits/others/custom_api",
)
def custom_api_tools() -> type[CustomApiTools]:
    """Return Custom API tools for making HTTP requests to external APIs."""
    from agno.tools.api import CustomApiTools

    return CustomApiTools
