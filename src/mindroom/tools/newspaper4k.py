"""Newspaper4k tool configuration."""

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
    from agno.tools.newspaper4k import Newspaper4kTools


@register_tool_with_metadata(
    name="newspaper4k",
    display_name="Newspaper4k",
    description="Read and extract content from news articles using advanced web scraping",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaNewspaper",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="include_summary",
            label="Include Summary",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="article_length",
            label="Article Length",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_read_article",
            label="Enable Read Article",
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
    dependencies=["newspaper4k", "lxml_html_clean"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/newspaper4k",
)
def newspaper4k_tools() -> type[Newspaper4kTools]:
    """Return Newspaper4k tools for news article extraction."""
    from agno.tools.newspaper4k import Newspaper4kTools

    return Newspaper4kTools
