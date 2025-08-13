"""Google Search tool configuration."""

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
    from agno.tools.googlesearch import GoogleSearchTools


@register_tool_with_metadata(
    name="googlesearch",
    display_name="Google Search",
    description="Search Google for web results using Python library",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaGoogle",
    icon_color="text-blue-500",
    config_fields=[
        # Search configuration
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
            required=False,
            default=None,
            placeholder="10",
            description="Fixed number of maximum search results to return (overrides per-query max_results)",
        ),
        ConfigField(
            name="fixed_language",
            label="Fixed Language",
            type="text",
            required=False,
            default=None,
            placeholder="en",
            description="Fixed language for search results (overrides per-query language setting)",
        ),
        ConfigField(
            name="proxy",
            label="Proxy",
            type="url",
            required=False,
            default=None,
            placeholder="http://proxy.example.com:8080",
            description="Proxy server for search requests",
        ),
        ConfigField(
            name="timeout",
            label="Request Timeout",
            type="number",
            required=False,
            default=10,
            placeholder="10",
            description="Timeout for search requests in seconds",
        ),
        ConfigField(
            name="headers",
            label="Custom Headers",
            type="text",
            required=False,
            default=None,
            placeholder='{"User-Agent": "Custom Agent"}',
            description="Custom headers for search requests (JSON format)",
        ),
    ],
    dependencies=["googlesearch-python", "pycountry"],
    docs_url="https://docs.agno.com/tools/toolkits/search/googlesearch",
)
def googlesearch_tools() -> type[GoogleSearchTools]:
    """Return Google Search tools for web search."""
    from agno.tools.googlesearch import GoogleSearchTools

    return GoogleSearchTools
