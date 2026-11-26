"""SearxNG tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.searxng import SearxngTools


@register_tool_with_metadata(
    name="searxng",
    file_access=ToolFileAccess.NONE,
    display_name="SearxNG",
    description="Open source search engine for web, images, news, science, and specialized content",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.NONE,
    icon="FaSearch",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="host",
            label="Host",
            type="url",
            required=True,
        ),
        ConfigField(
            name="engines",
            label="Engines",
            type="string[]",
            required=False,
        ),
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
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
    ],
    dependencies=[],  # httpx already included in main dependencies
    docs_url="https://docs.mindroom.chat/tools/web-search/#searxng",
    function_names=("searxng_search",),
)
def searxng_tools() -> type[SearxngTools]:
    """Return SearxNG search tools for web, images, news, and specialized content search."""
    from mindroom.custom_tools.searxng import SearxngTools

    return SearxngTools
