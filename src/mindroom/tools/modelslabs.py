"""ModelsLabs tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.modelslabs import ModelsLabTools


@register_tool_with_metadata(
    name="modelslabs",
    file_access=ToolFileAccess.NONE,
    display_name="ModelsLabs",
    description="AI model marketplace for generating images, videos, audio, and GIFs from text prompts",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Video",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("MODELS_LAB_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
            placeholder="your-modelslabs-api-key",
            description="The ModelsLab API key for authentication",
        ),
        ConfigField(
            name="file_type",
            label="File Type",
            type="text",
            required=False,
            default="mp4",
            placeholder="mp4",
            description="The type of file to generate (png, jpg, mp4, gif, mp3, or wav)",
        ),
        ConfigField(
            name="model_id",
            label="Model ID",
            type="text",
            required=False,
            default=None,
            placeholder="model-id",
            description="Optional ModelsLab model identifier override",
        ),
        ConfigField(
            name="width",
            label="Width",
            type="number",
            required=False,
            default=512,
            placeholder="512",
            description="Output image width in pixels",
        ),
        ConfigField(
            name="height",
            label="Height",
            type="number",
            required=False,
            default=512,
            placeholder="512",
            description="Output image height in pixels",
        ),
        ConfigField(
            name="wait_for_completion",
            label="Wait for Completion",
            type="boolean",
            required=False,
            default=False,
            description="Whether to wait for the media generation to complete before returning",
        ),
        ConfigField(
            name="add_to_eta",
            label="Add to ETA",
            type="number",
            required=False,
            default=15,
            placeholder="15",
            description="Extra one-second polling attempts added to the provider ETA; fractional attempt budgets round up",
        ),
        ConfigField(
            name="max_wait_time",
            label="Max Wait Time",
            type="number",
            required=False,
            default=60,
            placeholder="60",
            description="Cap on polling attempts, rounded up; zero skips polling and HTTP request time is additional",
        ),
    ],
    dependencies=["requests"],
    docs_url="https://docs.mindroom.chat/tools/ai-and-generation/#modelslabs",
    function_names=("generate_media",),
)
def modelslabs_tools() -> type[ModelsLabTools]:
    """Return ModelsLabs tool for AI-powered media generation."""
    from mindroom.custom_tools.modelslabs import ModelsLabTools

    return ModelsLabTools
