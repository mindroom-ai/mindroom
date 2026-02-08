"""Trafilatura tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.trafilatura import TrafilaturaTools


@register_tool_with_metadata(
    name="trafilatura",
    display_name="Trafilatura",
    description="Extract text and metadata from web pages, crawl websites, and convert HTML to text",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaFileAlt",
    icon_color="text-teal-500",
    config_fields=[
        ConfigField(
            name="output_format",
            label="Output Format",
            type="select",
            required=False,
            default="txt",
            options=[
                {"label": "Text", "value": "txt"},
                {"label": "JSON", "value": "json"},
                {"label": "Markdown", "value": "markdown"},
                {"label": "XML", "value": "xml"},
                {"label": "CSV", "value": "csv"},
                {"label": "HTML", "value": "html"},
            ],
        ),
        ConfigField(
            name="include_tables",
            label="Include Tables",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="include_links",
            label="Include Links",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="with_metadata",
            label="Include Metadata",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="target_language",
            label="Target Language",
            type="text",
            required=False,
            default=None,
            placeholder="e.g., en, de, fr (ISO 639-1)",
        ),
        ConfigField(
            name="max_crawl_urls",
            label="Max Crawl URLs",
            type="number",
            required=False,
            default=10,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["trafilatura"],
    docs_url="https://docs.agno.com/tools/toolkits/others/trafilatura",
)
def trafilatura_tools() -> type[TrafilaturaTools]:
    """Return Trafilatura tools for web content extraction."""
    from agno.tools.trafilatura import TrafilaturaTools

    return TrafilaturaTools
