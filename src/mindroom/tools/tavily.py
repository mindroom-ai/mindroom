"""Tavily tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.tavily import TavilyTools


@register_tool_with_metadata(
    name="tavily",
    display_name="Tavily",
    description="Real-time web search API for retrieving current information",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSearch",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="api_base_url",
            label="API Base URL",
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_context",
            label="Enable Search Context",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_extract",
            label="Enable Extract",
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
            name="max_tokens",
            label="Max Tokens",
            type="number",
            required=False,
            default=6000,
        ),
        ConfigField(
            name="include_answer",
            label="Include Answer",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="search_depth",
            label="Search Depth",
            type="text",
            required=False,
            default="advanced",
        ),
        ConfigField(
            name="extract_depth",
            label="Extract Depth",
            type="text",
            required=False,
            default="basic",
        ),
        ConfigField(
            name="include_images",
            label="Include Images",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="include_favicon",
            label="Include Favicon",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="extract_timeout",
            label="Extract Timeout",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="extract_format",
            label="Extract Format",
            type="text",
            required=False,
            default="markdown",
        ),
        ConfigField(
            name="format",
            label="Format",
            type="text",
            required=False,
            default="markdown",
        ),
        ConfigField(
            name="topic",
            label="Topic",
            type="text",
            required=False,
            default=None,
            description="Search category: general, news, or finance",
        ),
        ConfigField(
            name="time_range",
            label="Time Range",
            type="text",
            required=False,
            default=None,
            description="Time window for results: day, week, month, year (or d/w/m/y)",
        ),
        ConfigField(
            name="start_date",
            label="Start Date",
            type="text",
            required=False,
            default=None,
            description="Only include results published after this date (YYYY-MM-DD)",
        ),
        ConfigField(
            name="end_date",
            label="End Date",
            type="text",
            required=False,
            default=None,
            description="Only include results published before this date (YYYY-MM-DD)",
        ),
        ConfigField(
            name="days",
            label="Days",
            type="number",
            required=False,
            default=None,
            description="Number of days back to include results (news topic only)",
        ),
        ConfigField(
            name="include_domains",
            label="Include Domains",
            type="string[]",
            required=False,
            default=None,
            description="Restrict results to these domains",
        ),
        ConfigField(
            name="exclude_domains",
            label="Exclude Domains",
            type="string[]",
            required=False,
            default=None,
            description="Exclude these domains from results",
        ),
        ConfigField(
            name="country",
            label="Country",
            type="text",
            required=False,
            default=None,
            description="Boost results from this country (e.g. united states)",
        ),
        ConfigField(
            name="auto_parameters",
            label="Auto Parameters",
            type="boolean",
            required=False,
            default=False,
            description="Let Tavily auto-tune search parameters; explicitly set parameters take precedence",
        ),
        ConfigField(
            name="chunks_per_source",
            label="Chunks Per Source",
            type="number",
            required=False,
            default=None,
            description="Number of content chunks per source (1-3), advanced search only",
        ),
        ConfigField(
            name="search_params",
            label="Search Params",
            type="text",
            required=False,
            default=None,
            description="Additional parameters merged into every search request (JSON object)",
        ),
    ],
    dependencies=["tavily-python"],
    docs_url="https://docs.agno.com/tools/toolkits/search/tavily",
    function_names=("extract_url_content", "web_search_using_tavily", "web_search_with_tavily"),
)
def tavily_tools() -> type[TavilyTools]:
    """Return Tavily tools for real-time web search."""
    from agno.tools.tavily import TavilyTools

    return TavilyTools
