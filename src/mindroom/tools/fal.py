"""Fal tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.fal import FalTools


@register_tool_with_metadata(
    name="fal",
    display_name="Fal",
    description="AI model serving platform for media generation (images and videos)",
    category=ToolCategory.DEVELOPMENT,  # others category maps to DEVELOPMENT
    status=ToolStatus.REQUIRES_CONFIG,  # requires FAL_KEY API key
    setup_type=SetupType.API_KEY,  # uses API key authentication
    icon="FaRobot",  # AI/robot icon for AI model serving
    icon_color="text-purple-600",  # Purple for AI/ML services
    config_fields=[
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="fal-***",
            description="Fal API key for authentication (can also be set via FAL_KEY env var)",
        ),
        # Model configuration
        ConfigField(
            name="model",
            label="Model",
            type="text",
            required=False,
            default="fal-ai/hunyuan-video",
            placeholder="fal-ai/hunyuan-video",
            description="The model to use for media generation (default: fal-ai/hunyuan-video)",
        ),
    ],
    dependencies=["fal-client"],  # From agno requirements
    docs_url="https://docs.agno.com/tools/toolkits/others/fal",  # URL without .md extension
)
def fal_tools() -> type[FalTools]:
    """Return Fal tools for AI model serving and media generation."""
    from agno.tools.fal import FalTools

    return FalTools
