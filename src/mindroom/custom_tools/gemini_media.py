"""Native Google Gemini image and video generation toolkit."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

try:
    from google.genai import Client, types
except ImportError as exc:
    _msg = "`google-genai` not installed. Install with: `pip install google-genai`"
    raise ImportError(_msg) from exc

from mindroom.llm.media import Image, Video
from mindroom.logging_config import get_logger
from mindroom.model_defaults import GOOGLE_IMAGE, GOOGLE_VEO
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

# google-genai 3 routes the Gemini API to v1 by default, which omits preview image models; Vertex AI keeps its default.
_GEMINI_API_VERSION = "v1beta"


class GeminiTools(Toolkit):
    """Generate images and videos with Google Gemini and Veo models."""

    def __init__(
        self,
        api_key: str | None = None,
        vertexai: bool = False,
        project_id: str | None = None,
        location: str | None = None,
        image_generation_model: str = GOOGLE_IMAGE,
        video_generation_model: str = GOOGLE_VEO,
        enable_generate_image: bool = True,
        enable_generate_video: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        self.vertexai = vertexai
        self.project_id = project_id
        self.location = location
        self.api_key = api_key
        if not self.vertexai and not self.api_key:
            msg = "GOOGLE_API_KEY not set. Please provide api_key or set the environment variable."
            raise ValueError(msg)

        client_params: dict[str, Any] = {}
        if self.vertexai:
            client_params["vertexai"] = True
            client_params["project"] = self.project_id
            client_params["location"] = self.location
        else:
            client_params["api_key"] = self.api_key

        try:
            self.client: Any = Client(**client_params)
        except Exception as exc:
            msg = f"Failed to create Google GenAI Client. Error: {exc}"
            raise ValueError(msg) from exc

        self.image_model = image_generation_model
        self.video_model = video_generation_model

        tools: list[Callable[..., Any]] = []
        if all or enable_generate_image:
            tools.append(self.generate_image)
        if all or enable_generate_video:
            tools.append(self.generate_video)
        super().__init__(name="gemini", tools=tools)

    def generate_image(self, prompt: str) -> ToolValue:
        """Generate an image through the Gemini content-generation API.

        Args:
            prompt: Text description of the image to generate.

        """
        try:
            response = self.client.models.generate_content(
                model=self.image_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio="1:1"),
                    http_options=None if self.vertexai else types.HttpOptions(api_version=_GEMINI_API_VERSION),
                ),
            )
            generated_images = tuple(
                Image(
                    id=str(uuid4()),
                    content=part.inline_data.data,
                    original_prompt=prompt,
                    mime_type=part.inline_data.mime_type or "image/png",
                )
                for part in response.parts or ()
                if part.inline_data and part.inline_data.data
            )
            if not generated_images:
                return ToolValue(content="Failed to generate image: No images were generated.")
            return ToolValue(content="Image generated successfully", images=generated_images)
        except Exception as exc:
            logger.exception("Failed to generate image with Gemini")
            return ToolValue(content=f"Failed to generate image: {exc}")

    def generate_video(self, prompt: str) -> ToolValue:
        """Generate a video from a text prompt using Vertex AI Veo.

        Args:
            prompt: Text description of the video to generate.

        """
        if not self.vertexai:
            return ToolValue(
                content=(
                    "Video generation requires Vertex AI mode. "
                    "Please set `vertexai=True` or environment variable `GOOGLE_GENAI_USE_VERTEXAI=true`."
                ),
            )
        try:
            operation = self.client.models.generate_videos(
                model=self.video_model,
                prompt=prompt,
                config=types.GenerateVideosConfig(enhance_prompt=True),
            )
            while not operation.done:
                time.sleep(5)
                operation = self.client.operations.get(operation=operation)

            result = operation.result
            raw_videos = result.generated_videos if result and result.generated_videos else ()
            generated_videos = tuple(
                Video(
                    id=str(uuid4()),
                    content=item.video.video_bytes,
                    original_prompt=prompt,
                    mime_type=item.video.mime_type or "video/mp4",
                )
                for item in raw_videos
                if item.video and item.video.video_bytes
            )
            if not generated_videos:
                return ToolValue(content="Failed to generate video: No videos were generated.")
            return ToolValue(content="Video generated successfully", videos=generated_videos)
        except Exception as exc:
            logger.exception("Failed to generate video with Gemini")
            return ToolValue(content=f"Failed to generate video: {exc}")


__all__ = ["GeminiTools"]
