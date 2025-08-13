"""Oxylabs tool configuration."""

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
    from agno.tools.oxylabs import OxylabsTools


@register_tool_with_metadata(
    name="oxylabs",
    display_name="Oxylabs",
    description="Powerful web scraping capabilities including SERP, Amazon product data, and universal web scraping",
    category=ToolCategory.RESEARCH,  # web_scrape maps to RESEARCH
    status=ToolStatus.REQUIRES_CONFIG,  # requires username and password
    setup_type=SetupType.API_KEY,  # uses username/password credentials
    icon="FaGlobe",
    icon_color="text-blue-600",  # Web/scraping theme
    config_fields=[
        # Authentication credentials
        ConfigField(
            name="username",
            label="Username",
            type="text",
            required=False,
            placeholder="your_oxylabs_username",
            description="Oxylabs dashboard username (can also be set via OXYLABS_USERNAME env var)",
        ),
        ConfigField(
            name="password",
            label="Password",
            type="password",
            required=False,
            placeholder="your_oxylabs_password",
            description="Oxylabs dashboard password (can also be set via OXYLABS_PASSWORD env var)",
        ),
    ],
    dependencies=["oxylabs"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/oxylabs",
)
def oxylabs_tools() -> type[OxylabsTools]:
    """Return Oxylabs tools for web scraping and data extraction."""
    from agno.tools.oxylabs import OxylabsTools

    return OxylabsTools
