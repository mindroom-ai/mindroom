"""Replicate tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.model_defaults import REPLICATE_VIDEO
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.replicate import ReplicateTools


@register_tool_with_metadata(
    name="replicate",
    file_access=ToolFileAccess.NONE,
    display_name="Replicate",
    description="Generate images and videos using AI models on the Replicate platform",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaVideo",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("REPLICATE_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="model",
            label="Model",
            type="text",
            required=False,
            default=REPLICATE_VIDEO,
        ),
        ConfigField(
            name="enable_generate_media",
            label="Enable Generate Media",
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
    dependencies=["replicate"],
    docs_url="https://docs.mindroom.chat/tools/ai-and-generation/#replicate",
    function_names=("generate_media",),
)
def replicate_tools() -> type[ReplicateTools]:
    """Return Replicate tools for AI media generation."""
    from mindroom.custom_tools.replicate import ReplicateTools

    return ReplicateTools
