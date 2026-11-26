"""Native Luma AI Dream Machine video generation toolkit."""

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

_LumaAspectRatio = Literal["1:1", "16:9", "9:16", "4:3", "3:4", "21:9", "9:21"]
_LumaModel = Literal["ray-2", "ray-flash-2"]


class LumaLabTools(Toolkit):
    """Generate videos via Luma AI Dream Machine."""

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
        resolved_key = api_key.strip() if isinstance(api_key, str) else ""
        if not resolved_key:
            msg = "LUMAAI_API_KEY not set. Please set the LUMAAI_API_KEY environment variable."
            raise ValueError(msg)

        self.wait_for_completion = wait_for_completion
        self.poll_interval = poll_interval
        self.max_wait_time = max_wait_time
        self.api_key: str = resolved_key
        self.model: _LumaModel = "ray-2" if model is None else model
        self.client: Any = LumaAI(auth_token=self.api_key)

        tools: list[Callable[..., Any]] = []
        if all or enable_generate_video:
            tools.append(self.generate_video)
        if all or enable_image_to_video:
            tools.append(self.image_to_video)
        super().__init__(name="lumalabs", tools=tools)

    def generate_video(
        self,
        prompt: str,
        loop: bool = False,
        aspect_ratio: _LumaAspectRatio = "16:9",
        keyframes: dict[str, dict[str, str]] | None = None,
    ) -> ToolValue:
        """Generate a video from a text prompt and optional keyframes.

        Args:
            prompt: Text description of the desired video.
            loop: Whether the video should loop seamlessly.
            aspect_ratio: Output aspect ratio.
            keyframes: Optional raw keyframe mapping (e.g. `{"frame0": {"type": "image", "url": "..."}}`).

        """
        return self._submit_and_poll_video(
            prompt=prompt,
            loop=loop,
            aspect_ratio=aspect_ratio,
            keyframes=keyframes,
        )

    def image_to_video(
        self,
        prompt: str,
        start_image_url: str,
        end_image_url: str | None = None,
        loop: bool = False,
        aspect_ratio: _LumaAspectRatio = "16:9",
    ) -> ToolValue:
        """Generate a video from one or two images with a prompt.

        Args:
            prompt: Text description of the desired video.
            start_image_url: URL of the starting image.
            end_image_url: Optional URL of the ending image.
            loop: Whether the video should loop seamlessly.
            aspect_ratio: Output aspect ratio.

        """
        keyframes: dict[str, dict[str, str]] = {"frame0": {"type": "image", "url": start_image_url}}
        if end_image_url:
            keyframes["frame1"] = {"type": "image", "url": end_image_url}
        return self._submit_and_poll_video(
            prompt=prompt,
            loop=loop,
            aspect_ratio=aspect_ratio,
            keyframes=keyframes,
        )

    def _submit_and_poll_video(
        self,
        *,
        prompt: str,
        loop: bool,
        aspect_ratio: _LumaAspectRatio,
        keyframes: dict[str, dict[str, str]] | None = None,
    ) -> ToolValue:
        try:
            params: dict[str, Any] = {
                "model": self.model,
                "prompt": prompt,
                "loop": loop,
                "aspect_ratio": aspect_ratio,
            }
            if keyframes is not None:
                params["keyframes"] = keyframes

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
