"""Website tools configuration."""

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
    from agno.tools.website import WebsiteTools


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
            name="knowledge_base",
            label="Knowledge Base",
            type="text",
            required=False,
            default=None,
            description="Advanced: Optional knowledge base instance for storing website content (WebsiteKnowledgeBase or CombinedKnowledgeBase)",
        ),
    ],
    dependencies=["httpx", "beautifulsoup4"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/website",
)
def website_tools() -> type[WebsiteTools]:
    """Return website tools for web scraping and content extraction."""
    from agno.tools.website import WebsiteTools

    return WebsiteTools
