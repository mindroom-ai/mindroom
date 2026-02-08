"""Notion tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.notion import NotionTools


@register_tool_with_metadata(
    name="notion",
    display_name="Notion",
    description="Create, update, and search pages in Notion databases",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="SiNotion",
    icon_color="text-gray-800",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=True,
            placeholder="Notion integration token",
            description="Internal integration token (falls back to NOTION_API_KEY env var)",
        ),
        ConfigField(
            name="database_id",
            label="Database ID",
            type="text",
            required=True,
            placeholder="Notion database ID",
            description="ID of the Notion database to work with (falls back to NOTION_DATABASE_ID env var)",
        ),
        ConfigField(
            name="enable_create_page",
            label="Enable Create Page",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_update_page",
            label="Enable Update Page",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_pages",
            label="Enable Search Pages",
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
    dependencies=["notion-client"],
    docs_url="https://docs.agno.com/tools/toolkits/others/notion",
    helper_text="Create an integration at [Notion Developers](https://www.notion.so/my-integrations) and share the database with it",
)
def notion_tools() -> type[NotionTools]:
    """Return Notion tools for page management."""
    from agno.tools.notion import NotionTools

    return NotionTools
