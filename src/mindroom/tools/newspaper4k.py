"""Newspaper4k tool configuration."""

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
        # Article reading functionality
        ConfigField(
            name="read_article",
            label="Read Article",
            type="boolean",
            required=False,
            default=True,
            description="Enable the functionality to read the full content of an article",
        ),
        # Content options
        ConfigField(
            name="include_summary",
            label="Include Summary",
            type="boolean",
            required=False,
            default=False,
            description="Include a summary of the article along with the full content",
        ),
        ConfigField(
            name="article_length",
            label="Article Length",
            type="number",
            required=False,
            placeholder="5000",
            description="Maximum length of the article or its summary to be processed or returned",
        ),
    ],
    dependencies=["newspaper4k", "lxml_html_clean"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/newspaper4k",
)
def newspaper4k_tools() -> type[Newspaper4kTools]:
    """Return Newspaper4k tools for news article extraction."""
    from agno.tools.newspaper4k import Newspaper4kTools

    return Newspaper4kTools
