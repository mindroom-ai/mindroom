"""Website tools configuration."""

from __future__ import annotations

from mindroom.custom_tools.website import WebsiteTools
from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata


@register_tool_with_metadata(
    name="website",
    display_name="Website Tools",
    description="Web scraping and content extraction from websites",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaGlobe",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="knowledge",
            label="Knowledge",
            type="text",
            required=False,
            default=None,
        ),
    ],
    dependencies=["httpx", "beautifulsoup4"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/website",
    function_names=("add_website_to_knowledge", "read_url"),
)
def website_tools() -> type[WebsiteTools]:
    """Return website tools for web scraping and content extraction."""
    return WebsiteTools
