"""DALL-E tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.dalle import DalleTools


@register_tool_with_metadata(
    name="dalle",
    display_name="DALL-E",
    description="OpenAI DALL-E image generation from text prompts",
    category=ToolCategory.DEVELOPMENT,  # others/ maps to DEVELOPMENT
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API key
    setup_type=SetupType.API_KEY,  # Uses OpenAI API key
    icon="FaImage",  # React icon for image generation
    icon_color="text-green-600",  # OpenAI brand color
    config_fields=[
        ConfigField(
            name="model",
            label="Model",
            type="text",
            required=False,
            default="dall-e-3",
        ),
        ConfigField(
            name="n",
            label="N",
            type="number",
            required=False,
            default=1,
        ),
        ConfigField(
            name="size",
            label="Size",
            type="text",
            required=False,
            default="1024x1024",
        ),
        ConfigField(
            name="quality",
            label="Quality",
            type="text",
            required=False,
            default="standard",
        ),
        ConfigField(
            name="style",
            label="Style",
            type="text",
            required=False,
            default="vivid",
        ),
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="enable_create_image",
            label="Enable Create Image",
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
    dependencies=["openai"],  # OpenAI Python package
    docs_url="https://docs.agno.com/tools/toolkits/others/dalle",  # URL without .md extension
)
def dalle_tools() -> type[DalleTools]:
    """Return DALL-E tools for image generation from text prompts."""
    from agno.tools.dalle import DalleTools

    return DalleTools
