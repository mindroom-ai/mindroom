"""Searxng tool configuration."""

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
    from agno.tools.searxng import Searxng


@register_tool_with_metadata(
    name="searxng",
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
            type="text",
            required=False,
        ),
        ConfigField(
            name="fixed_max_results",
            label="Fixed Max Results",
            type="number",
            required=False,
            default=None,
        ),
    ],
    dependencies=[],  # httpx already included in main dependencies
    docs_url="https://docs.agno.com/tools/toolkits/search/searxng",
)
def searxng_tools() -> type[Searxng]:
    """Return SearxNG search tools for web, images, news, and specialized content search."""
    from agno.tools.searxng import Searxng

    return Searxng
