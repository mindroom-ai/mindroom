"""Native Replicate media generation toolkit bound to the toolkit's configured API key."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

try:
    import replicate
    from replicate.helpers import FileOutput
except ImportError as exc:
    msg = "`replicate` not installed. Install with: `pip install replicate`"
    raise ImportError(msg) from exc

from mindroom.llm.media import Image, Video
from mindroom.logging_config import get_logger
from mindroom.model_defaults import REPLICATE_VIDEO
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue

logger = get_logger(__name__)

_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".webp"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm"})


def _parse_file_output(output: FileOutput) -> tuple[str, Image | Video]:
    ext = Path(urlparse(output.url).path).suffix.lower()
    media_id = str(uuid4())
    if ext in _IMAGE_EXTENSIONS:
        return f"Image generated successfully at {output.url}", Image(id=media_id, url=output.url)
    if ext in _VIDEO_EXTENSIONS:
        return f"Video generated successfully at {output.url}", Video(id=media_id, url=output.url)
    msg = f"Unsupported media type with extension '{ext}'."
    raise ValueError(msg)


class ReplicateTools(Toolkit):
    """Generate images or videos using a configured Replicate model."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = REPLICATE_VIDEO,
        enable_generate_media: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        self.api_key = api_key or os.getenv("REPLICATE_API_KEY")
        if not self.api_key:
            logger.error("REPLICATE_API_KEY not set")
        self.model = model

        tools: list[Callable[..., Any]] = []
        if all or enable_generate_media:
            tools.append(self.generate_media)
        super().__init__(name="replicate", tools=tools)

    def generate_media(self, prompt: str) -> ToolValue:
        """Generate an image or video using the configured Replicate model.

        Args:
            prompt: Text description of the media to generate.

        """
        if not self.api_key:
            logger.error("replicate_api_key_missing")
            return ToolValue(content="API key is not set.")

        try:
            client = replicate.Client(api_token=self.api_key)
            # Replicate 1.0.7 has no public close/context-manager API; keep the underlying httpx client
            # open across iterator materialization and always close it on exit.
            with client._client:
                outputs = client.run(ref=self.model, input={"prompt": prompt})
                if isinstance(outputs, FileOutput):
                    materialized = [outputs]
                elif isinstance(outputs, (Iterable, Iterator)) and not isinstance(outputs, str):
                    materialized = list(outputs)
                else:
                    logger.error("replicate_unexpected_output_type", output_type=type(outputs).__name__)
                    return ToolValue(content=f"Unexpected output type: {type(outputs)}")

                images: list[Image] = []
                videos: list[Video] = []
                results: list[str] = []
                for item in materialized:
                    if not isinstance(item, FileOutput):
                        logger.error("replicate_unexpected_output_type", output_type=type(item).__name__)
                        return ToolValue(content=f"Unexpected output type: {type(item)}")
                    message, artifact = _parse_file_output(item)
                    results.append(message)
                    if isinstance(artifact, Image):
                        images.append(artifact)
                    else:
                        videos.append(artifact)

                return ToolValue(
                    content="\n".join(results),
                    images=tuple(images),
                    videos=tuple(videos),
                )
        except Exception as exc:
            logger.exception("replicate_generation_failed")
            return ToolValue(content=f"Error: {exc}")
