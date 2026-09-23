"""Gemini media toolkit using native Gemini image generation."""

from __future__ import annotations

from typing import TYPE_CHECKING, override
from uuid import uuid4

from agno.media import Image
from agno.tools.function import ToolResult
from agno.tools.models.gemini import GeminiTools
from agno.utils.log import log_debug, log_error, log_info
from google.genai import types

if TYPE_CHECKING:
    from agno.agent import Agent


class MindRoomGeminiTools(GeminiTools):
    """Preserve Agno's Gemini toolkit contract while using Nano Banana image models."""

    @override
    def generate_image(
        self,
        agent: Agent,
        prompt: str,
    ) -> ToolResult:
        """Generate an image through the Gemini content-generation API."""
        try:
            response = self.client.models.generate_content(
                model=self.image_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    image_config=types.ImageConfig(aspect_ratio="1:1"),
                ),
            )
            generated_images = [
                Image(
                    id=str(uuid4()),
                    content=part.inline_data.data,
                    original_prompt=prompt,
                    mime_type=part.inline_data.mime_type or "image/png",
                )
                for part in response.parts or []
                if part.inline_data and part.inline_data.data
            ]
            if not generated_images:
                log_info("No images were generated.")
                return ToolResult(content="Failed to generate image: No images were generated.")

            log_debug(f"Generated {len(generated_images)} image(s) with model {self.image_model}")
            return ToolResult(
                content="Image generated successfully",
                images=generated_images,
            )
        except Exception as exc:
            log_error(f"Failed to generate image: {exc}")
            return ToolResult(content=f"Failed to generate image: {exc}")


__all__ = ["MindRoomGeminiTools"]
