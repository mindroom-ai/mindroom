"""Crawl4AI tool configuration."""

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
    from agno.tools.crawl4ai import Crawl4aiTools


@register_tool_with_metadata(
    name="crawl4ai",
    display_name="Crawl4AI",
    description="Web crawling and scraping using the Crawl4ai library",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaSpider",
    icon_color="text-blue-600",
    config_fields=[
        # Content extraction settings
        ConfigField(
            name="max_length",
            label="Max Length",
            type="number",
            required=False,
            default=5000,
            placeholder="5000",
            description="Maximum length of the text from the webpage to be returned",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=60,
            placeholder="60",
            description="Timeout in seconds for page loading",
        ),
        # Content filtering settings
        ConfigField(
            name="use_pruning",
            label="Use Pruning",
            type="boolean",
            required=False,
            default=False,
            description="Enable content pruning to remove low-quality content",
        ),
        ConfigField(
            name="pruning_threshold",
            label="Pruning Threshold",
            type="number",
            required=False,
            default=0.48,
            placeholder="0.48",
            description="Threshold for content pruning (0.0 to 1.0)",
        ),
        ConfigField(
            name="bm25_threshold",
            label="BM25 Threshold",
            type="number",
            required=False,
            default=1.0,
            placeholder="1.0",
            description="Threshold for BM25 content filtering when using search queries",
        ),
        # Browser settings
        ConfigField(
            name="headless",
            label="Headless Mode",
            type="boolean",
            required=False,
            default=True,
            description="Run browser in headless mode (no GUI)",
        ),
        ConfigField(
            name="wait_until",
            label="Wait Until",
            type="text",
            required=False,
            default="domcontentloaded",
            placeholder="domcontentloaded",
            description="Browser event to wait for before extracting content (domcontentloaded, load, networkidle)",
        ),
    ],
    dependencies=["crawl4ai"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/crawl4ai",
)
def crawl4ai_tools() -> type[Crawl4aiTools]:
    """Return Crawl4AI tools for web crawling and scraping."""
    from agno.tools.crawl4ai import Crawl4aiTools

    return Crawl4aiTools
