"""BaiduSearch tool configuration."""

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
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="fixed_language",
            label="Fixed Language",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="headers",
            label="Headers",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="proxy",
            label="Proxy",
            type="url",
            required=False,
            default=None,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=10,
        ),
        ConfigField(
            name="debug",
            label="Debug",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_baidu_search",
            label="Enable Baidu Search",
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
    dependencies=["baidusearch", "pycountry"],
    docs_url="https://docs.agno.com/tools/toolkits/search/baidusearch",
)
def baidusearch_tools() -> type[BaiduSearchTools]:
    """Return Baidu search tools for web search."""
    from agno.tools.baidusearch import BaiduSearchTools

    return BaiduSearchTools
