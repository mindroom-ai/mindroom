"""ModelsLabs tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from agno.tools.models_labs import ModelsLabTools


@register_tool_with_metadata(
    name="modelslabs",
    display_name="ModelsLabs",
    description="AI model marketplace for generating images, videos, audio, and GIFs from text prompts",
    category=ToolCategory.DEVELOPMENT,  # From docs URL: /tools/toolkits/others/
    status=ToolStatus.REQUIRES_CONFIG,  # Requires API key
    setup_type=SetupType.API_KEY,  # Uses api_key parameter
    icon="Video",  # React icon for video generation
    icon_color="text-purple-600",  # Purple color for creative/AI tools
    config_fields=[
        # Authentication parameter
        ConfigField(
            name="api_key",
            label="API Key",
            type="password",
            required=False,
            default=None,
            placeholder="your-modelslabs-api-key",
            description="The ModelsLab API key for authentication",
        ),
        # Media generation parameters
        ConfigField(
            name="file_type",
            label="File Type",
            type="text",
            required=False,
            # The agno default is FileType.MP4, which has value "mp4"
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
        # Timing and completion parameters
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
            description="Time in seconds to add to the ETA to account for the time it takes to fetch the media",
        ),
        ConfigField(
            name="max_wait_time",
            label="Max Wait Time",
            type="number",
            required=False,
            default=60,
            placeholder="60",
            description="Maximum time in seconds to wait for the media to be ready",
        ),
    ],
    dependencies=["requests"],  # Already in pyproject.toml
    docs_url="https://docs.agno.com/tools/toolkits/others/models_labs",
    function_names=("generate_media",),
)
def modelslabs_tools() -> type[ModelsLabTools]:
    """Return ModelsLabs tool for AI-powered media generation."""
    from agno.models.response import FileType
    from agno.tools.models_labs import ModelsLabTools

    class MindRoomModelsLabTools(ModelsLabTools):
        """ModelsLab toolkit that accepts authored string file types."""

        def __init__(
            self,
            api_key: str | None = None,
            wait_for_completion: bool = False,
            add_to_eta: int = 15,
            max_wait_time: int = 60,
            file_type: FileType | str = FileType.MP4,
            model_id: str | None = None,
            width: int = 512,
            height: int = 512,
            **kwargs: object,
        ) -> None:
            resolved_file_type = FileType(file_type.lower()) if isinstance(file_type, str) else file_type
            super().__init__(
                api_key=api_key,
                wait_for_completion=wait_for_completion,
                add_to_eta=add_to_eta,
                max_wait_time=max_wait_time,
                file_type=resolved_file_type,
                model_id=model_id,
                width=width,
                height=height,
                **kwargs,
            )

    MindRoomModelsLabTools.__init__.__annotations__["file_type"] = FileType | str
    return MindRoomModelsLabTools
