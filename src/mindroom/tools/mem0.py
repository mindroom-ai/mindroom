"""Mem0 Memory tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.mem0 import Mem0Tools


@register_tool_with_metadata(
    name="mem0",
    display_name="Mem0 Memory",
    description="Persistent memory system that stores, retrieves, searches, and manages user memories and context",
    category=ToolCategory.PRODUCTIVITY,  # Database tools → Productivity
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API key for cloud usage
    setup_type=SetupType.API_KEY,  # Optional API key for cloud usage
    icon="Brain",
    icon_color="text-purple-600",  # Memory/brain theme
    config_fields=[
        ConfigField(
            name="config",
            label="Config",
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
            name="user_id",
            label="User ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="org_id",
            label="Org ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="project_id",
            label="Project ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="infer",
            label="Infer",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_add_memory",
            label="Enable Add Memory",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_memory",
            label="Enable Search Memory",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_all_memories",
            label="Enable Get All Memories",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_delete_all_memories",
            label="Enable Delete All Memories",
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
    dependencies=["mem0ai"],  # Already in pyproject.toml
    docs_url="https://docs.agno.com/tools/toolkits/database/mem0",
)
def mem0_tools() -> type[Mem0Tools]:
    """Return Mem0 memory tools for persistent memory management."""
    from agno.tools.mem0 import Mem0Tools

    return Mem0Tools
