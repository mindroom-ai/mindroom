"""Mem0 Memory tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

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
        # Authentication/Connection parameters first
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="mem0_...",
            description="Mem0 API key for cloud usage (can also be set via MEM0_API_KEY env var)",
        ),
        ConfigField(
            name="org_id",
            label="Organization ID",
            type="text",
            required=False,
            placeholder="org_123...",
            description="Organization ID for Mem0 cloud (can also be set via MEM0_ORG_ID env var)",
        ),
        ConfigField(
            name="project_id",
            label="Project ID",
            type="text",
            required=False,
            placeholder="proj_456...",
            description="Project ID for Mem0 cloud (can also be set via MEM0_PROJECT_ID env var)",
        ),
        # Configuration parameters
        ConfigField(
            name="config",
            label="Local Configuration",
            type="text",
            required=False,
            placeholder='{"vector_store": {"provider": "chroma"}}',
            description="JSON configuration for self-hosted Mem0 instance (overrides cloud API)",
        ),
        ConfigField(
            name="user_id",
            label="Default User ID",
            type="text",
            required=False,
            placeholder="user_123",
            description="Default user ID for memory operations (can be overridden per operation)",
        ),
        # Feature parameters
        ConfigField(
            name="infer",
            label="Enable Memory Inference",
            type="boolean",
            required=False,
            default=True,
            description="Enable automatic memory inference and extraction from conversations",
        ),
    ],
    dependencies=["mem0ai"],  # Already in pyproject.toml
    docs_url="https://docs.agno.com/tools/toolkits/database/mem0",
)
def mem0_tools() -> type[Mem0Tools]:
    """Return Mem0 memory tools for persistent memory management."""
    from agno.tools.mem0 import Mem0Tools

    return Mem0Tools
