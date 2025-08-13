"""BaiduSearch tool configuration."""

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
    from agno.tools.baidusearch import BaiduSearchTools


@register_tool_with_metadata(
    name="baidusearch",
    display_name="Baidu Search",
    description="Search the web using Baidu search engine with Chinese language support",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Search",
    icon_color="text-blue-600",
    config_fields=[
        # Search configuration
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
            required=False,
            placeholder="5",
            description="Sets a fixed number of maximum results to return",
        ),
        ConfigField(
            name="fixed_language",
            label="Fixed Language",
            type="text",
            required=False,
            placeholder="zh",
            description="Set the fixed language for the results",
        ),
        ConfigField(
            name="headers",
            label="Custom Headers",
            type="text",
            required=False,
            placeholder='{"User-Agent": "Custom Agent"}',
            description="Headers to be used in the search request",
        ),
        ConfigField(
            name="proxy",
            label="Proxy",
            type="url",
            required=False,
            placeholder="http://proxy.example.com:8080",
            description="Specifies a single proxy address as a string",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=10,
            description="Sets the timeout for HTTP requests, in seconds",
        ),
        # Feature flags
        ConfigField(
            name="debug",
            label="Debug Mode",
            type="boolean",
            required=False,
            default=False,
            description="Enable debug output for search requests",
        ),
    ],
    dependencies=["baidusearch", "pycountry"],
    docs_url="https://docs.agno.com/tools/toolkits/search/baidusearch",
)
def baidusearch_tools() -> type[BaiduSearchTools]:
    """Return Baidu search tools for web search."""
    from agno.tools.baidusearch import BaiduSearchTools

    return BaiduSearchTools
