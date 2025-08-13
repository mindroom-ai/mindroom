"""DALL-E tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

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
        # Authentication parameter first
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="sk-...",
            description="OpenAI API key for authentication (can also be set via OPENAI_API_KEY env var)",
        ),
        # Model configuration
        ConfigField(
            name="model",
            label="Model",
            type="text",
            required=False,
            default="dall-e-3",
            placeholder="dall-e-3",
            description="The DALL-E model to use (dall-e-3 or dall-e-2)",
        ),
        ConfigField(
            name="size",
            label="Image Size",
            type="text",
            required=False,
            default="1024x1024",
            placeholder="1024x1024",
            description="Image size (256x256, 512x512, 1024x1024, 1792x1024, or 1024x1792)",
        ),
        ConfigField(
            name="quality",
            label="Quality",
            type="text",
            required=False,
            default="standard",
            placeholder="standard",
            description="Image quality (standard or hd)",
        ),
        ConfigField(
            name="style",
            label="Style",
            type="text",
            required=False,
            default="vivid",
            placeholder="vivid",
            description="Image style (vivid or natural)",
        ),
        ConfigField(
            name="n",
            label="Number of Images",
            type="number",
            required=False,
            default=1,
            description="Number of images to generate (DALL-E 3 only supports 1)",
        ),
    ],
    dependencies=["openai"],  # OpenAI Python package
    docs_url="https://docs.agno.com/tools/toolkits/others/dalle",  # URL without .md extension
)
def dalle_tools() -> type[DalleTools]:
    """Return DALL-E tools for image generation from text prompts."""
    from agno.tools.dalle import DalleTools

    return DalleTools
