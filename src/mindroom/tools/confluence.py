"""Confluence tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.confluence import ConfluenceTools


@register_tool_with_metadata(
    name="confluence",
    display_name="Confluence",
    description="Atlassian wiki platform for retrieving, creating, and updating pages",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaConfluence",
    icon_color="text-blue-600",
    config_fields=[
        # Authentication/Connection parameters
        ConfigField(
            name="url",
            label="Confluence URL",
            type="url",
            required=False,
            placeholder="https://your-confluence-instance.atlassian.net",
            description="Confluence instance URL (can also be set via CONFLUENCE_URL env var)",
        ),
        ConfigField(
            name="username",
            label="Username",
            type="text",
            required=False,
            placeholder="your-username",
            description="Confluence username (can also be set via CONFLUENCE_USERNAME env var)",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            placeholder="your-password",
            description="Confluence password (can also be set via CONFLUENCE_PASSWORD env var)",
        ),
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="your-api-key",
            description="Confluence API key - alternative to password (can also be set via CONFLUENCE_API_KEY env var)",
        ),
        # Configuration options
        ConfigField(
            name="verify_ssl",
            label="Verify SSL",
            type="boolean",
            required=False,
            default=True,
            description="Whether to verify SSL certificates when connecting to Confluence",
        ),
    ],
    dependencies=["atlassian-python-api"],
    docs_url="https://docs.agno.com/tools/toolkits/others/confluence",
)
def confluence_tools() -> type[ConfluenceTools]:
    """Return Confluence tools for wiki management."""
    from agno.tools.confluence import ConfluenceTools

    return ConfluenceTools
