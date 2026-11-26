"""Native Luma Labs Dream Machine video generation toolkit."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

try:
    from lumaai import LumaAI
except ImportError as exc:
    _msg = "`lumaai` not installed. Install with: `pip install lumaai`"
    raise ImportError(_msg) from exc

from mindroom.llm.media import Video
from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_LumaModel = Literal["ray-2", "ray-flash-2"]
_LumaAspectRatio = Literal["1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "9:21"]


class LumaLabTools(Toolkit):
    """Generate videos from text prompts or keyframe images using Luma Dream Machine."""

    def __init__(
        self,
        api_key: str | None = None,
        wait_for_completion: bool = True,
        poll_interval: int = 3,
        max_wait_time: int = 300,
        enable_generate_video: bool = True,
        enable_image_to_video: bool = True,
        all: bool = False,  # noqa: A002
        model: _LumaModel | None = "ray-2",
    ) -> None:
        self.wait_for_completion = wait_for_completion
        self.poll_interval = poll_interval
        self.max_wait_time = max_wait_time
        self.api_key = api_key
        self.model: _LumaModel = "ray-2" if model is None else model
        self._allow_text_to_video = all or enable_generate_video
        self._allow_image_to_video = all or enable_image_to_video

        if not self.api_key:
            logger.error("LUMAAI_API_KEY not set")

        self.client: Any = LumaAI(auth_token=self.api_key)

        tools: list[Callable[..., Any]] = []
        if self._allow_text_to_video or self._allow_image_to_video:
            tools.append(self.generate_video)
        super().__init__(name="lumalabs", tools=tools)

    def generate_video(
        self,
        prompt: str,
        start_image_url: str | None = None,
        end_image_url: str | None = None,
        loop: bool = False,
        aspect_ratio: _LumaAspectRatio = "16:9",
        keyframes: dict[str, dict[str, str]] | None = None,
    ) -> ToolValue:
        """Generate a video from a text prompt and optional start/end images or keyframes.

        Args:
            prompt: Text description of the desired video.
            start_image_url: Optional starting keyframe image URL (image-to-video).
            end_image_url: Optional ending keyframe image URL (requires `start_image_url`).
            loop: Whether the video should loop seamlessly.
            aspect_ratio: Output aspect ratio.
            keyframes: Optional raw keyframe mapping (e.g. `{"frame0": {"type": "image", "url": "..."}}`).

        """
        if end_image_url and not start_image_url:
            return ToolValue(content="Error: end_image_url requires start_image_url.")

        resolved_keyframes = keyframes
        if start_image_url:
            resolved_keyframes = {"frame0": {"type": "image", "url": start_image_url}}
            if end_image_url:
                resolved_keyframes["frame1"] = {"type": "image", "url": end_image_url}

        has_images = bool(resolved_keyframes)
        if has_images and not self._allow_image_to_video:
            return ToolValue(content="Error: Image-to-video generation is disabled in configuration.")
        if not has_images and not self._allow_text_to_video:
            return ToolValue(
                content="Error: Text-to-video generation is disabled; provide start_image_url or keyframes.",
            )

        try:
            params: dict[str, Any] = {
                "model": self.model,
                "prompt": prompt,
                "loop": loop,
                "aspect_ratio": aspect_ratio,
            }
            if resolved_keyframes is not None:
                params["keyframes"] = resolved_keyframes

            generation = self.client.generations.create(**params)
            return self._poll_generation(generation)
        except Exception as exc:
            logger.exception("Failed to generate Luma video")
            return ToolValue(content=f"Error: {exc}")

    def _poll_generation(self, generation: Any) -> ToolValue:  # noqa: ANN401
        if not self.wait_for_completion:
            return ToolValue(content="Async generation unsupported")
        video_id = str(uuid4())
        seconds_waited = 0
        while seconds_waited < self.max_wait_time:
            if not generation or not generation.id:
                return ToolValue(content="Failed to get generation ID")
            generation = self.client.generations.get(generation.id)
            if generation.state == "completed" and generation.assets and (video_url := generation.assets.video):
                video = Video(id=video_id, url=video_url, eta="completed")
                return ToolValue(content=f"Video generated successfully: {video_url}", videos=(video,))
            if generation.state == "failed":
                return ToolValue(content=f"Generation failed: {generation.failure_reason}")
            time.sleep(self.poll_interval)
            seconds_waited += self.poll_interval
        return ToolValue(content=f"Video generation timed out after {self.max_wait_time} seconds")
