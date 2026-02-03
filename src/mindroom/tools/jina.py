"""Jina Reader tool configuration."""

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
    from agno.tools.jina import JinaReaderTools


@register_tool_with_metadata(
    name="jina",
    display_name="Jina Reader",
    description="Web content reading and search using Jina AI Reader API",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaGlobe",
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
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default="https://r.jina.ai/",
        ),
        ConfigField(
            name="search_url",
            label="Search URL",
            type="url",
            required=False,
            default="https://s.jina.ai/",
        ),
        ConfigField(
            name="max_content_length",
            label="Max Content Length",
            type="number",
            required=False,
            default=10000,
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            default=None,
        ),
        ConfigField(
            name="search_query_content",
            label="Search Query Content",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_read_url",
            label="Enable Read URL",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_query",
            label="Enable Search Query",
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
    ],
    dependencies=["httpx", "pydantic"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/jina_reader",
)
def jina_tools() -> type[JinaReaderTools]:
    """Return Jina Reader tools for web content reading and search."""
    from agno.tools.jina import JinaReaderTools

    return JinaReaderTools
