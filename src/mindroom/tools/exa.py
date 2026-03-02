"""Exa tool configuration."""

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
    from agno.tools.exa import ExaTools


@register_tool_with_metadata(
    name="exa",
    display_name="Exa",
    description="Advanced AI-powered web search engine for research and content discovery",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSearch",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="enable_search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_contents",
            label="Enable Get Contents",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_find_similar",
            label="Enable Find Similar",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_answer",
            label="Enable Answer",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_research",
            label="Enable Research",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="text",
            label="Text",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="text_length_limit",
            label="Text Length Limit",
            type="number",
            required=False,
            default=1000,
        ),
        ConfigField(
            name="summary",
            label="Summary",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="num_results",
            label="Num Results",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="livecrawl",
            label="Livecrawl",
            type="text",
            required=False,
            default="always",
        ),
        ConfigField(
            name="start_crawl_date",
            label="Start Crawl Date",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="end_crawl_date",
            label="End Crawl Date",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="start_published_date",
            label="Start Published Date",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="end_published_date",
            label="End Published Date",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="type",
            label="Type",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="category",
            label="Category",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="include_domains",
            label="Include Domains",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="exclude_domains",
            label="Exclude Domains",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="show_results",
            label="Show Results",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="model",
            label="Model",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=30,
        ),
        ConfigField(
            name="research_model",
            label="Research Model",
            type="text",
            required=False,
            default="exa-research",
        ),
    ],
    dependencies=["exa_py"],
    docs_url="https://docs.agno.com/tools/toolkits/search/exa",
)
def exa_tools() -> type[ExaTools]:
    """Return Exa tools for AI-powered web search and research."""
    from agno.tools.exa import ExaTools

    return ExaTools
