"""Google Search tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.duckduckgo import ddgs_config_fields

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
    config_fields=ddgs_config_fields(
        (
            "enable_search",
            "enable_news",
            "modifier",
            "fixed_max_results",
            "proxy",
            "timeout",
            "verify_ssl",
            "timelimit",
            "region",
        ),
    ),
    dependencies=["ddgs"],
    docs_url="https://docs.mindroom.chat/tools/web-search/#googlesearch",
    function_names=("google_search",),
    action_scoped_grants=("google_search",),
)
def googlesearch_tools() -> type[GoogleSearchTools]:
    """Return Google Search tools for web search."""
    from mindroom.custom_tools.ddgs_search import GoogleSearchTools

    return GoogleSearchTools
