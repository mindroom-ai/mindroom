"""Replicate tool configuration."""

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
    from agno.tools.replicate import ReplicateTools


@register_tool_with_metadata(
    name="replicate",
    display_name="Replicate",
    description="Generate images and videos using AI models on the Replicate platform",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaVideo",
    icon_color="text-purple-600",
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="r8_...",
            description="Replicate API token (can also be set via REPLICATE_API_TOKEN env var)",
        ),
        # Model configuration
        ConfigField(
            name="model",
            label="Model",
            type="text",
            required=False,
            default="minimax/video-01",
            placeholder="minimax/video-01",
            description="The Replicate model to use for media generation",
        ),
    ],
    dependencies=["replicate"],
    docs_url="https://docs.agno.com/tools/toolkits/others/replicate",
)
def replicate_tools() -> type[ReplicateTools]:
    """Return Replicate tools for AI media generation."""
    from agno.tools.replicate import ReplicateTools

    return ReplicateTools
