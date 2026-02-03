"""Luma Labs tool configuration."""

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
    from agno.tools.lumalab import LumaLabTools


@register_tool_with_metadata(
    name="lumalabs",
    display_name="Luma Labs",
    description="3D content creation and video generation using Luma AI Dream Machine",
    category=ToolCategory.DEVELOPMENT,  # others/ category maps to DEVELOPMENT
    status=ToolStatus.REQUIRES_CONFIG,  # Requires LUMAAI_API_KEY
    setup_type=SetupType.API_KEY,  # API key authentication
    icon="FaVideo",  # Video-related icon
    icon_color="text-purple-600",  # Purple color for AI/ML tools
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
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
    docs_url="https://docs.agno.com/tools/toolkits/others/lumalabs",
)
def lumalabs_tools() -> type[LumaLabTools]:
    """Return Luma Labs tools for 3D content creation and video generation."""
    from agno.tools.lumalab import LumaLabTools

    return LumaLabTools
