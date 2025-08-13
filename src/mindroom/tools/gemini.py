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
        # Authentication
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            placeholder="AIza...",
            description="Google AI API key for authentication (can also be set via GOOGLE_API_KEY env var)",
        ),
        # Vertex AI configuration
        ConfigField(
            name="vertexai",
            label="Use Vertex AI",
            type="boolean",
            required=False,
            default=False,
            description="Enable Vertex AI API instead of Gemini API (required for video generation)",
        ),
        ConfigField(
            name="project_id",
            label="Project ID",
            type="text",
            required=False,
            placeholder="my-project-123",
            description="Google Cloud project ID (for Vertex AI, can also be set via GOOGLE_CLOUD_PROJECT env var)",
        ),
        ConfigField(
            name="location",
            label="Location",
            type="text",
            required=False,
            default="us-central1",
            placeholder="us-central1",
            description="Google Cloud location/region (for Vertex AI, can also be set via GOOGLE_CLOUD_LOCATION env var)",
        ),
        # Model configuration
        ConfigField(
            name="image_generation_model",
            label="Image Generation Model",
            type="text",
            required=False,
            default="imagen-3.0-generate-002",
            placeholder="imagen-3.0-generate-002",
            description="Model to use for image generation",
        ),
        ConfigField(
            name="video_generation_model",
            label="Video Generation Model",
            type="text",
            required=False,
            default="veo-2.0-generate-001",
            placeholder="veo-2.0-generate-001",
            description="Model to use for video generation (requires Vertex AI)",
        ),
    ],
    dependencies=["google-genai"],
    docs_url="https://docs.agno.com/tools/toolkits/models/gemini",
)
def gemini_tools() -> type[GeminiTools]:
    """Return Gemini tools for image and video generation."""
    from agno.tools.models.gemini import GeminiTools

    return GeminiTools
