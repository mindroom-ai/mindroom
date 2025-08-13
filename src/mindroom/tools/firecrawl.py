"""Firecrawl tool configuration."""

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
    from agno.tools.firecrawl import FirecrawlTools


@register_tool_with_metadata(
    name="firecrawl",
    display_name="Firecrawl",
    description="Web scraping and crawling tool for extracting content from websites",
    category=ToolCategory.RESEARCH,  # Web scraping tool for research
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API key
    setup_type=SetupType.API_KEY,  # API key authentication
    icon="FaSpider",  # Web crawler icon
    icon_color="text-orange-500",  # Orange color for fire/crawling theme
    config_fields=[
        # Authentication/Connection parameters first
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="fc-...",
            description="Firecrawl API key for authentication (can also be set via FIRECRAWL_API_KEY env var)",
        ),
        ConfigField(
            name="api_url",
            label="API URL",
            type="url",
            required=False,
            default="https://api.firecrawl.dev",
            placeholder="https://api.firecrawl.dev",
            description="Firecrawl API endpoint URL",
        ),
        # Configuration parameters
        ConfigField(
            name="formats",
            label="Output Formats",
            type="text",
            required=False,
            placeholder="markdown,html,text",
            description="Comma-separated list of output formats (e.g., markdown, html, text)",
        ),
        ConfigField(
            name="limit",
            label="Crawl Limit",
            type="number",
            required=False,
            default=10,
            placeholder="10",
            description="Maximum number of pages to crawl",
        ),
        ConfigField(
            name="poll_interval",
            label="Poll Interval",
            type="number",
            required=False,
            default=30,
            placeholder="30",
            description="Polling interval in seconds for crawl operations",
        ),
        ConfigField(
            name="search_params",
            label="Search Parameters",
            type="text",
            required=False,
            placeholder='{"location": "US", "language": "en"}',
            description="Additional search parameters as JSON string",
        ),
        # Feature flags/boolean parameters grouped by functionality
        # Core Operations
        ConfigField(
            name="scrape",
            label="Enable Scraping",
            type="boolean",
            required=False,
            default=True,
            description="Enable website scraping functionality",
        ),
        ConfigField(
            name="crawl",
            label="Enable Crawling",
            type="boolean",
            required=False,
            default=False,
            description="Enable website crawling functionality",
        ),
        ConfigField(
            name="mapping",
            label="Enable Mapping",
            type="boolean",
            required=False,
            default=False,
            description="Enable website structure mapping functionality",
        ),
        ConfigField(
            name="search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=False,
            description="Enable web search functionality",
        ),
    ],
    dependencies=["firecrawl-py"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/firecrawl",
)
def firecrawl_tools() -> type[FirecrawlTools]:
    """Return Firecrawl tools for web scraping and crawling."""
    from agno.tools.firecrawl import FirecrawlTools

    return FirecrawlTools
