"""Newspaper tool configuration."""

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
    from agno.tools.newspaper import NewspaperTools


@register_tool_with_metadata(
    name="newspaper",
    display_name="News Articles",
    description="Extract and analyze news articles from URLs",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Newspaper",
    icon_color="text-gray-600",
    config_fields=[
        # Article extraction
        ConfigField(
            name="get_article_text",
            label="Get Article Text",
            type="boolean",
            required=False,
            default=True,
            description="Enable extracting text content from article URLs",
        ),
    ],
    dependencies=["newspaper3k", "lxml_html_clean"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/newspaper",
)
def newspaper_tools() -> type[NewspaperTools]:
    """Return newspaper tools for article extraction."""
    from agno.tools.newspaper import NewspaperTools

    return NewspaperTools
