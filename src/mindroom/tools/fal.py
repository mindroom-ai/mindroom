"""Fal tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.model_defaults import FAL_VIDEO
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.fal import FalTools


@register_tool_with_metadata(
    name="fal",
    file_access=ToolFileAccess.NONE,
    display_name="Fal",
    description="AI model serving platform for media generation (images and videos)",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaRobot",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("FAL_API_KEY", "FAL_KEY"),
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
            default=FAL_VIDEO,
        ),
        bool_config_field("enable_generate_media", "Enable Generate Media"),
        bool_config_field("enable_image_to_image", "Enable Image To Image", default=False),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["fal-client"],
    docs_url="https://docs.mindroom.chat/tools/ai-and-generation/#fal",
    function_names=("generate_media", "image_to_image"),
)
def fal_tools() -> type[FalTools]:
    """Return Fal tools for AI model serving and media generation."""
    from mindroom.custom_tools.fal import FalTools

    return FalTools
