"""Fal media toolkit adapter for current image editing models."""

from __future__ import annotations

from typing import override
from uuid import uuid4

import fal_client
from agno.agent import Agent  # noqa: TC002  # resolved by Agno function schema introspection
from agno.media import Image, Video
from agno.team.team import Team  # noqa: TC002  # resolved by Agno function schema introspection
from agno.tools.fal import FalTools
from agno.tools.function import ToolResult
from agno.utils.log import log_error, logger

_FAL_IMAGE_EDIT_ENDPOINT = "fal-ai/flux-2/edit"


class MindRoomFalTools(FalTools):
    """Use current Fal endpoints with configured client credentials."""

    @override
    def generate_media(  # noqa: C901  # mirrors upstream media response shapes
        self,
        agent: Agent | Team,
        prompt: str,
    ) -> ToolResult:
        """Generate media with the configured Fal model.

        Args:
            agent: Calling agent or team, injected by Agno.
            prompt: Text describing the media to generate.

        """
        del agent

        try:
            result = fal_client.SyncClient(key=self.api_key).subscribe(
                self.model,
                arguments={"prompt": prompt},
                with_logs=True,
                on_queue_update=self.on_queue_update,
            )

            if "images" in result and isinstance(result["images"], list) and result["images"]:
                images = []
                urls = []
                for image_data in result["images"]:
                    url = image_data.get("url", "")
                    if url:
                        urls.append(url)
                        images.append(Image(id=str(uuid4()), url=url))

                if images:
                    return ToolResult(
                        content=f"Generated {len(images)} image(s) successfully: {', '.join(urls)}",
                        images=images,
                    )
            elif "image" in result:
                url = result.get("image", {}).get("url", "")
                return ToolResult(
                    content=f"Image generated successfully at {url}",
                    images=[Image(id=str(uuid4()), url=url)],
                )
            elif "videos" in result and isinstance(result["videos"], list) and result["videos"]:
                videos = []
                urls = []
                for video_data in result["videos"]:
                    url = video_data.get("url", "")
                    if url:
                        urls.append(url)
                        videos.append(Video(id=str(uuid4()), url=url))

                if videos:
                    return ToolResult(
                        content=f"Generated {len(videos)} video(s) successfully: {', '.join(urls)}",
                        videos=videos,
                    )
            elif "video" in result:
                url = result.get("video", {}).get("url", "")
                return ToolResult(
                    content=f"Video generated successfully at {url}",
                    videos=[Video(id=str(uuid4()), url=url)],
                )

            log_error(f"Unsupported type in result: {result}")
            return ToolResult(content=f"Unsupported type in result: {result}")
        except Exception as exc:
            logger.exception("Failed to run model")
            return ToolResult(content=f"Error: {exc}")

    @override
    def image_to_image(
        self,
        agent: Agent | Team,
        prompt: str,
        image_url: str | None = None,
    ) -> ToolResult:
        """Transform an input image according to a text prompt.

        Args:
            agent: Calling agent or team, injected by Agno.
            prompt: Text describing the desired changes.
            image_url: URL of the image to edit.

        """
        del agent

        if not image_url:
            return ToolResult(content="Error: image_url is required for image editing.")

        try:
            result = fal_client.SyncClient(key=self.api_key).subscribe(
                _FAL_IMAGE_EDIT_ENDPOINT,
                arguments={"image_urls": [image_url], "prompt": prompt},
                with_logs=True,
                on_queue_update=self.on_queue_update,
            )
            url = result["images"][0]["url"]
            image_artifact = Image(id=str(uuid4()), url=url)
            return ToolResult(content=f"Image generated successfully at {url}", images=[image_artifact])
        except Exception as exc:
            logger.exception("Failed to generate image")
            return ToolResult(content=f"Error: {exc}")


__all__ = ["MindRoomFalTools"]
