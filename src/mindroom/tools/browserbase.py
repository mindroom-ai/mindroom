"""Browserbase tool configuration."""

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
    from agno.tools.browserbase import BrowserbaseTools


@register_tool_with_metadata(
    name="browserbase",
    display_name="Browserbase",
    description="Browser automation and web scraping using headless browsers",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaChrome",
    icon_color="text-blue-600",
    config_fields=[
        # Authentication/Connection parameters
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="bb_...",
            description="Browserbase API key (can also be set via BROWSERBASE_API_KEY env var)",
        ),
        ConfigField(
            name="project_id",
            label="Project ID",
            type="text",
            required=False,
            placeholder="project-12345",
            description="Browserbase project ID (can also be set via BROWSERBASE_PROJECT_ID env var)",
        ),
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            placeholder="https://api.browserbase.com",
            description="Custom Browserbase API endpoint URL. Only use this if you're using a self-hosted Browserbase instance or need to connect to a different region (can also be set via BROWSERBASE_BASE_URL env var)",
        ),
    ],
    dependencies=["browserbase", "playwright"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/browserbase",
)
def browserbase_tools() -> type[BrowserbaseTools]:
    """Return Browserbase tools for browser automation and web scraping."""
    from agno.tools.browserbase import BrowserbaseTools

    return BrowserbaseTools
