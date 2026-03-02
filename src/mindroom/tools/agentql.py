"""AgentQL tool configuration."""

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
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_scrape_website",
            label="Enable Scrape Website",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_custom_scrape_website",
            label="Enable Custom Scrape Website",
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
        ConfigField(
            name="agentql_query",
            label="Agentql Query",
            type="text",
            required=False,
            default="",
        ),
    ],
    dependencies=["agentql", "playwright"],
    docs_url="https://docs.agno.com/tools/toolkits/web_scrape/agentql",
)
def agentql_tools() -> type[AgentQLTools]:
    """Return AgentQL tools for AI-powered web scraping."""
    from agno.tools.agentql import AgentQLTools

    return AgentQLTools
