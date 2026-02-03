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
        ConfigField(
            name="max_length",
            label="Max Length",
            type="number",
            required=False,
            default=5000,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=60,
        ),
        ConfigField(
            name="use_pruning",
            label="Use Pruning",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="pruning_threshold",
            label="Pruning Threshold",
            type="number",
            required=False,
            default=0.48,
        ),
        ConfigField(
            name="bm25_threshold",
            label="Bm25 Threshold",
            type="number",
            required=False,
            default=1.0,
        ),
        ConfigField(
            name="headless",
            label="Headless",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="wait_until",
            label="Wait Until",
            type="text",
            required=False,
            default="domcontentloaded",
        ),
        ConfigField(
            name="proxy_config",
            label="Proxy Config",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_crawl",
            label="Enable Crawl",
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
    ],
    dependencies=["crawl4ai"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/crawl4ai",
)
def crawl4ai_tools() -> type[Crawl4aiTools]:
    """Return Crawl4AI tools for web crawling and scraping."""
    from agno.tools.crawl4ai import Crawl4aiTools

    return Crawl4aiTools
