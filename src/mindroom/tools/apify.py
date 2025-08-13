"""Apify tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.apify import ApifyTools


@register_tool_with_metadata(
    name="apify",
    display_name="Apify",
    description="Web scraping, crawling, data extraction, and web automation platform with ready-to-use Actors",
    category=ToolCategory.DEVELOPMENT,  # Based on agno docs URL path 'others/'
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API token
    setup_type=SetupType.API_KEY,  # Uses API token authentication
    icon="FaCode",  # Web scraping/automation icon
    icon_color="text-blue-600",  # Apify brand color
    config_fields=[
        # Authentication
        ConfigField(
            name="apify_api_token",
            label="API Token",
            type="password",
            required=False,
            placeholder="apify_api_...",
            description="Apify API token for authentication (can also be set via APIFY_API_TOKEN env var)",
        ),
        # Configuration
        ConfigField(
            name="actors",
            label="Actors",
            type="text",
            required=False,
            placeholder="apify/rag-web-browser,compass/crawler-google-places",
            description="Single Actor ID as string or comma-separated list of Actor IDs to register as individual tools (e.g., 'apify/rag-web-browser' for web content extraction)",
        ),
    ],
    dependencies=["apify-client"],  # Required dependency for Apify integration
    docs_url="https://docs.agno.com/tools/toolkits/others/apify",
)
def apify_tools() -> type[ApifyTools]:
    """Return Apify tools for web scraping and automation."""
    from agno.tools.apify import ApifyTools

    return ApifyTools
