"""Zep memory system tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.zep import ZepTools


@register_tool_with_metadata(
    name="zep",
    display_name="Zep Memory",
    description="Memory system for storing, retrieving, and searching conversational data",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Brain",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="session_id",
            label="Session ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="user_id",
            label="User ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="ignore_assistant_messages",
            label="Ignore Assistant Messages",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_add_zep_message",
            label="Enable Add Zep Message",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_zep_memory",
            label="Enable Get Zep Memory",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_zep_memory",
            label="Enable Search Zep Memory",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="instructions",
            label="Instructions",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="add_instructions",
            label="Add Instructions",
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
    dependencies=["zep-cloud"],
    docs_url="https://docs.agno.com/tools/toolkits/database/zep",
)
def zep_tools() -> type[ZepTools]:
    """Return Zep memory tools for storing and retrieving conversational data."""
    from agno.tools.zep import ZepTools

    return ZepTools
