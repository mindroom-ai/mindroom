"""Luma Labs tool configuration."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Literal

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

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
    docs_url="https://docs.agno.com/tools/toolkits/others/lumalabs",
    function_names=("generate_video", "image_to_video"),
)
def lumalabs_tools() -> type[LumaLabTools]:
    """Return Luma Labs tools for 3D content creation and video generation."""
    from agno.tools.lumalab import LumaLabTools

    class MindRoomLumaLabTools(LumaLabTools):
        """Luma toolkit with a configured model for both generation methods."""

        def __init__(
            self,
            api_key: str | None = None,
            wait_for_completion: bool = True,
            poll_interval: int = 3,
            max_wait_time: int = 300,
            enable_generate_video: bool = True,
            enable_image_to_video: bool = True,
            all: bool = False,  # noqa: A002 - Preserve the upstream toolkit configuration key.
            model: Literal["ray-2", "ray-flash-2"] | None = "ray-2",
            **kwargs: object,
        ) -> None:
            super().__init__(
                api_key=api_key,
                wait_for_completion=wait_for_completion,
                poll_interval=poll_interval,
                max_wait_time=max_wait_time,
                enable_generate_video=enable_generate_video,
                enable_image_to_video=enable_image_to_video,
                all=all,
                **kwargs,
            )
            if model is None:
                model = "ray-2"
            # AGNO_COMPAT: Luma video creation omits the SDK's required model.
            # Reason: Agno 3.0.9 omits model in both creation methods; lumaai 1.21.0 requires it.
            # Upstream issue: Tracking gap; no matching issue has been verified for this omission.
            # Upstream PR: No matching fix has been verified.
            # Remove when: Agno forwards a configurable model to both creation paths;
            # retain MindRoom's configured default, polling, media, and error behavior.
            # Coverage: tests/test_lumalabs_tool.py::test_lumalabs_generation_sends_model_and_returns_video;
            # tests/test_lumalabs_tool.py preserves completion controls, HTTP errors, and registration flags.
            # Intentional instance-local SDK method binding; its declared method type cannot express this repair.
            self.client.generations.create = partial(self.client.generations.create, model=model)  # ty: ignore[invalid-assignment]

    return MindRoomLumaLabTools
