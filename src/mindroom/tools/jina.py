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
        # Authentication/Connection parameters first
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="jina_...",
            description="API key for Jina Reader (can also be set via JINA_API_KEY env var)",
        ),
        ConfigField(
            name="base_url",
            label="Base URL",
            type="url",
            required=False,
            default="https://r.jina.ai/",
            placeholder="https://r.jina.ai/",
            description="Base URL for Jina Reader API",
        ),
        ConfigField(
            name="search_url",
            label="Search URL",
            type="url",
            required=False,
            default="https://s.jina.ai/",
            placeholder="https://s.jina.ai/",
            description="Search URL for Jina Reader API",
        ),
        # Configuration parameters
        ConfigField(
            name="max_content_length",
            label="Max Content Length",
            type="number",
            required=False,
            default=10000,
            description="Maximum content length in characters",
        ),
        ConfigField(
            name="timeout",
            label="Timeout",
            type="number",
            required=False,
            placeholder="30",
            description="Timeout for Jina Reader API requests in seconds",
        ),
        # Feature flags grouped by functionality
        # URL Reading Features
        ConfigField(
            name="read_url",
            label="Read URL",
            type="boolean",
            required=False,
            default=True,
            description="Enable URL reading functionality",
        ),
        # Search Features
        ConfigField(
            name="search_query",
            label="Search Query",
            type="boolean",
            required=False,
            default=False,
            description="Enable web search functionality",
        ),
        ConfigField(
            name="search_query_content",
            label="Search Query Content",
            type="boolean",
            required=False,
            default=True,
            description="Include full URL content in search results",
        ),
    ],
    dependencies=["httpx", "pydantic"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/jina_reader",
)
def jina_tools() -> type[JinaReaderTools]:
    """Return Jina Reader tools for web content reading and search."""
    from agno.tools.jina import JinaReaderTools

    return JinaReaderTools
