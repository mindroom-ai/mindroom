"""Website tools configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.website import WebsiteTools


@register_tool_with_metadata(
    name="website",
    file_access=ToolFileAccess.NONE,
    display_name="Website Tools",
    description="Web scraping and content extraction from websites",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaGlobe",
    icon_color="text-blue-600",
    dependencies=["httpx", "beautifulsoup4"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/website",
    function_names=("read_url",),
)
def website_tools() -> type[WebsiteTools]:
    """Return website tools for web scraping and content extraction."""
    from mindroom.custom_tools.website import WebsiteTools

    return WebsiteTools
