"""Native Fal AI media generation and image editing toolkit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

try:
    import fal_client
except ImportError as exc:
    _msg = "`fal_client` not installed. Install with: `pip install fal-client`"
    raise ImportError(_msg) from exc

from mindroom.llm.media import Image, Video
from mindroom.logging_config import get_logger
from mindroom.model_defaults import FAL_VIDEO
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_FAL_IMAGE_EDIT_ENDPOINT = "fal-ai/flux-2/edit"


class FalTools(Toolkit):
    """Generate images/videos and edit images via Fal AI endpoints."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = FAL_VIDEO,
        enable_generate_media: bool = True,
        enable_image_to_image: bool = False,
        all: bool = False,  # noqa: A002
    ) -> None:
        self.api_key = api_key
        if not self.api_key:
            logger.error("FAL_API_KEY not set")
        self.model = model
        self.seen_logs: set[str] = set()

        tools: list[Callable[..., Any]] = []
        if all or enable_generate_media:
            tools.append(self.generate_media)
        if all or enable_image_to_image:
            tools.append(self.image_to_image)
        super().__init__(name="fal", tools=tools)

    def on_queue_update(self, update: object) -> None:
        """Log newly emitted Fal queue progress lines once."""
        if isinstance(update, fal_client.InProgress) and update.logs:
            for log_entry in update.logs:
                message = log_entry["message"]
                if message not in self.seen_logs:
                    logger.info("fal_queue_log", message=message)
                    self.seen_logs.add(message)

    def generate_media(self, prompt: str) -> ToolValue:
        """Generate an image or video with the configured Fal model.

        Args:
            prompt: Text description of the media to generate.

        """
        try:
            result = fal_client.SyncClient(key=self.api_key).subscribe(
                self.model,
                arguments={"prompt": prompt},
                with_logs=True,
                on_queue_update=self.on_queue_update,
            )
            return _parse_fal_media_result(result)
        except Exception as exc:
            logger.exception("Failed to run Fal model", model=self.model)
            return ToolValue(content=f"Error: {exc}")

    def image_to_image(self, prompt: str, image_url: str | None = None) -> ToolValue:
        """Edit an input image according to a text prompt using Fal Flux 2.

        Args:
            prompt: Text describing the desired edits.
            image_url: URL of the source image to edit.

        """
        if not image_url:
            return ToolValue(content="Error: image_url is required for image editing.")
        try:
            result = fal_client.SyncClient(key=self.api_key).subscribe(
                _FAL_IMAGE_EDIT_ENDPOINT,
                arguments={"image_urls": [image_url], "prompt": prompt},
                with_logs=True,
                on_queue_update=self.on_queue_update,
            )
            url = result["images"][0]["url"]
            return ToolValue(
                content=f"Image generated successfully at {url}",
                images=(Image(id=str(uuid4()), url=url),),
            )
        except Exception as exc:
            logger.exception("Failed to edit image with Fal")
            return ToolValue(content=f"Error: {exc}")


def _parse_fal_media_result(result: dict[str, Any]) -> ToolValue:
    if isinstance(result.get("images"), list) and result["images"]:
        urls = [entry.get("url", "") for entry in result["images"] if entry.get("url")]
        if urls:
            return ToolValue(
                content=f"Generated {len(urls)} image(s) successfully: {', '.join(urls)}",
                images=tuple(Image(id=str(uuid4()), url=url) for url in urls),
            )
    if "image" in result:
        url = result.get("image", {}).get("url", "")
        if url:
            return ToolValue(
                content=f"Image generated successfully at {url}",
                images=(Image(id=str(uuid4()), url=url),),
            )
    if isinstance(result.get("videos"), list) and result["videos"]:
        urls = [entry.get("url", "") for entry in result["videos"] if entry.get("url")]
        if urls:
            return ToolValue(
                content=f"Generated {len(urls)} video(s) successfully: {', '.join(urls)}",
                videos=tuple(Video(id=str(uuid4()), url=url) for url in urls),
            )
    if "video" in result:
        url = result.get("video", {}).get("url", "")
        if url:
            return ToolValue(
                content=f"Video generated successfully at {url}",
                videos=(Video(id=str(uuid4()), url=url),),
            )
    logger.error("fal_unsupported_result", result=result)
    return ToolValue(content=f"Unsupported type in result: {result}")


__all__ = ["FalTools"]
