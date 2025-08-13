"""AgentQL tool configuration."""

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
    from agno.tools.agentql import AgentQLTools


@register_tool_with_metadata(
    name="agentql",
    display_name="AgentQL",
    description="AI-powered web scraping and data extraction from websites",
    category=ToolCategory.RESEARCH,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaSpider",
    icon_color="text-purple-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="agentql_...",
            description="AgentQL API key for authentication (can also be set via AGENTQL_API_KEY env var)",
        ),
        # Feature flags
        ConfigField(
            name="scrape",
            label="Enable Text Scraping",
            type="boolean",
            required=False,
            default=True,
            description="Enable the basic text scraping functionality",
        ),
        ConfigField(
            name="agentql_query",
            label="Custom AgentQL Query",
            type="text",
            required=False,
            default="",
            placeholder='{"links": ["a"], "titles": ["h1", "h2"]}',
            description="Custom AgentQL query for specific data extraction (enables custom scraping when provided)",
        ),
    ],
    dependencies=["agentql", "playwright"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/agentql",
)
def agentql_tools() -> type[AgentQLTools]:
    """Return AgentQL tools for AI-powered web scraping."""
    from agno.tools.agentql import AgentQLTools

    return AgentQLTools
