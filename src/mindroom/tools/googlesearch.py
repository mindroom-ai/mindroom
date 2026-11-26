"""Google Search tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.ddgs_search import GoogleSearchTools


@register_tool_with_metadata(
    name="googlesearch",
    file_access=ToolFileAccess.NONE,
    display_name="Google Search",
    description="Search web and news through DDGS with a requested Google backend and automatic fallback",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="FaGoogle",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="enable_search",
            label="Enable Search",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_news",
            label="Enable News",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="modifier",
            label="Modifier",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
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
            name="verify_ssl",
            label="Verify Ssl",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="timelimit",
            label="Time Limit",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="region",
            label="Region",
            type="text",
            required=False,
            default=None,
        ),
    ],
    dependencies=["ddgs"],
    docs_url="https://docs.mindroom.chat/tools/web-search/#googlesearch",
    function_names=("web_search", "search_news"),
)
def googlesearch_tools() -> type[GoogleSearchTools]:
    """Return Google Search tools for web search."""
    from mindroom.custom_tools.ddgs_search import GoogleSearchTools

    return GoogleSearchTools
