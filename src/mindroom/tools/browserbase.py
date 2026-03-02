"""Browserbase tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import (
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
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="project_id",
            label="Project ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_navigate_to",
            label="Enable Navigate To",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_screenshot",
            label="Enable Screenshot",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_page_content",
            label="Enable Get Page Content",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_close_session",
            label="Enable Close Session",
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
        ConfigField(
            name="parse_html",
            label="Parse Html",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="max_content_length",
            label="Max Content Length",
            type="number",
            required=False,
            default=100000,
        ),
    ],
    dependencies=["browserbase", "playwright"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/browserbase",
)
def browserbase_tools() -> type[BrowserbaseTools]:
    """Return Browserbase tools for browser automation and web scraping."""
    from agno.tools.browserbase import BrowserbaseTools

    return BrowserbaseTools
