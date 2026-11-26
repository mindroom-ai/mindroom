"""Luma Labs tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.lumalabs import LumaLabTools


@register_tool_with_metadata(
    name="lumalabs",
    file_access=ToolFileAccess.NONE,
    display_name="Luma Labs",
    description="3D content creation and video generation using Luma AI Dream Machine",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaVideo",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("LUMAAI_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="model",
            label="Model",
            type="select",
            required=False,
            default="ray-2",
            options=[
                {"label": "Ray 2", "value": "ray-2"},
                {"label": "Ray 2 Flash", "value": "ray-flash-2"},
            ],
            description="Dream Machine video model used for text and image generation requests",
        ),
        ConfigField(
            name="wait_for_completion",
            label="Wait For Completion",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="poll_interval",
            label="Poll Interval",
            type="number",
            required=False,
            default=3,
        ),
        ConfigField(
            name="max_wait_time",
            label="Max Wait Time",
            type="number",
            required=False,
            default=300,
        ),
        ConfigField(
            name="enable_generate_video",
            label="Enable Generate Video",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_image_to_video",
            label="Enable Image To Video",
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
    dependencies=["lumaai"],
    docs_url="https://docs.mindroom.chat/tools/ai-and-generation/#lumalabs",
    function_names=("generate_video", "image_to_video"),
)
def lumalabs_tools() -> type[LumaLabTools]:
    """Return Luma Labs tools for Dream Machine video generation."""
    from mindroom.custom_tools.lumalabs import LumaLabTools

    return LumaLabTools
