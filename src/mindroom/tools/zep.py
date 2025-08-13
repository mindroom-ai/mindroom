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
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="your_zep_api_key",
            description="Zep API key for authentication (can also be set via ZEP_API_KEY env var)",
        ),
        # Session and user configuration
        ConfigField(
            name="session_id",
            label="Session ID",
            type="text",
            required=False,
            placeholder="agno-session",
            description="Optional session ID. Auto-generated if not provided",
        ),
        ConfigField(
            name="user_id",
            label="User ID",
            type="text",
            required=False,
            placeholder="user-123",
            description="Optional user ID. Auto-generated if not provided",
        ),
        ConfigField(
            name="instructions",
            label="Custom Instructions",
            type="text",
            required=False,
            placeholder="Custom instructions for using the Zep tools",
            description="Custom instructions for using the Zep tools",
        ),
        # Feature toggles
        ConfigField(
            name="ignore_assistant_messages",
            label="Ignore Assistant Messages",
            type="boolean",
            required=False,
            default=False,
            description="Whether to ignore assistant messages when adding to memory",
        ),
        ConfigField(
            name="add_instructions",
            label="Add Instructions",
            type="boolean",
            required=False,
            default=False,
            description="Whether to add default instructions",
        ),
        # Memory operations
        ConfigField(
            name="add_zep_message",
            label="Add Zep Message",
            type="boolean",
            required=False,
            default=True,
            description="Enable adding messages to the current Zep session memory",
        ),
        ConfigField(
            name="get_zep_memory",
            label="Get Zep Memory",
            type="boolean",
            required=False,
            default=True,
            description="Enable retrieving memory for the current Zep session",
        ),
        ConfigField(
            name="search_zep_memory",
            label="Search Zep Memory",
            type="boolean",
            required=False,
            default=True,
            description="Enable searching the Zep memory store for relevant information",
        ),
    ],
    dependencies=["zep-cloud"],
    docs_url="https://docs.agno.com/tools/toolkits/database/zep",
)
def zep_tools() -> type[ZepTools]:
    """Return Zep memory tools for storing and retrieving conversational data."""
    from agno.tools.zep import ZepTools

    return ZepTools
