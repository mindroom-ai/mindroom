"""Gemini tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import ConfigField, SetupType, ToolCategory, ToolStatus, register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.models.gemini import GeminiTools


@register_tool_with_metadata(
    name="gemini",
    display_name="Gemini",
    description="Google AI API services for generating images and videos using Gemini models",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaGoogle",
    icon_color="text-blue-500",
    config_fields=[
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="vertexai",
            label="Vertexai",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="project_id",
            label="Project ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="location",
            label="Location",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="image_generation_model",
            label="Image Generation Model",
            type="text",
            required=False,
            default="imagen-3.0-generate-002",
        ),
        ConfigField(
            name="video_generation_model",
            label="Video Generation Model",
            type="text",
            required=False,
            default="veo-2.0-generate-001",
        ),
        ConfigField(
            name="enable_generate_image",
            label="Enable Generate Image",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_generate_video",
            label="Enable Generate Video",
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
    dependencies=["google-genai"],
    docs_url="https://docs.agno.com/tools/toolkits/models/gemini",
)
def gemini_tools() -> type[GeminiTools]:
    """Return Gemini tools for image and video generation."""
    from agno.tools.models.gemini import GeminiTools

    return GeminiTools
