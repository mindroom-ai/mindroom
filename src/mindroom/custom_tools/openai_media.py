"""Native OpenAI media toolkit for transcription, image generation, and speech synthesis."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path  # noqa: TC003 - toolkit introspection evaluates constructor annotations.
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

try:
    from openai import OpenAI as OpenAIClient
except ImportError as exc:
    msg = "`openai` not installed. Install with: `pip install openai`"
    raise ImportError(msg) from exc

from mindroom.file_access import resolve_agent_file
from mindroom.llm.media import Audio, Image
from mindroom.logging_config import get_logger
from mindroom.model_defaults import OPENAI_IMAGE, OPENAI_TRANSCRIPTION, OPENAI_TTS
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue
from mindroom.voice.openai_realtime_speech import is_realtime_speech_model, pcm_to_wav, synthesize_realtime_speech

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.models import FileAccess

logger = get_logger(__name__)

OpenAIImageSize = Literal["256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"]
OpenAIImageStyle = Literal["vivid", "natural"]


class OpenAITools(Toolkit):
    """OpenAI transcription, image generation, and speech synthesis toolkit."""

    def __init__(
        self,
        api_key: str | None = None,
        enable_transcription: bool = True,
        enable_image_generation: bool = True,
        enable_speech_generation: bool = True,
        all: bool = False,  # noqa: A002
        transcription_model: str = OPENAI_TRANSCRIPTION,
        text_to_speech_voice: str = "alloy",
        text_to_speech_model: str = OPENAI_TTS,
        text_to_speech_format: str = "wav",
        image_model: str | None = OPENAI_IMAGE,
        image_quality: str | None = None,
        image_size: OpenAIImageSize | None = None,
        image_style: OpenAIImageStyle | None = None,
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
    ) -> None:
        resolved_key = api_key or os.getenv("OPENAI_API_KEY")
        if not resolved_key:
            msg = "OPENAI_API_KEY not set. Please set the OPENAI_API_KEY environment variable."
            raise ValueError(msg)

        self.api_key: str = resolved_key
        self.transcription_model = transcription_model
        self.tts_voice = text_to_speech_voice
        self.tts_model = text_to_speech_model
        self.tts_format = text_to_speech_format
        self.image_model = image_model
        self.image_quality = image_quality
        self.image_size = image_size
        self.image_style = image_style
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access

        tools: list[Callable[..., Any]] = []
        if all or enable_transcription:
            tools.append(self.transcribe_audio)
        if all or enable_image_generation:
            tools.append(self.generate_image)
        if all or enable_speech_generation:
            tools.append(self.generate_speech)
        super().__init__(name="openai", tools=tools)

    def transcribe_audio(self, audio_path: str) -> str:
        """Transcribe an audio file using OpenAI speech-to-text.

        Args:
            audio_path: Local audio file path (confined to workspace unless `file_access: unrestricted`).

        """
        try:
            authorized = resolve_agent_file(
                audio_path,
                workspace_root=self._workspace_root,
                file_access=self._file_access,
                field_name="audio_path",
            )
            with authorized.open() as audio_file:
                transcript = OpenAIClient(api_key=self.api_key).audio.transcriptions.create(
                    model=self.transcription_model,
                    file=(authorized.name, audio_file),
                    response_format="text",
                )
        except Exception as exc:
            logger.exception("Failed to transcribe audio")
            return f"Failed to transcribe audio: {exc}"
        return str(transcript)

    def generate_image(self, prompt: str) -> ToolValue:
        """Generate an image from a text prompt using OpenAI.

        Args:
            prompt: Text description of the image to generate.

        """
        try:
            extra_params: dict[str, Any] = {
                key: value
                for key, value in (
                    ("size", self.image_size),
                    ("quality", self.image_quality),
                    ("style", self.image_style),
                )
                if value is not None
            }
            if not (self.image_model and self.image_model.startswith("gpt-image")):
                extra_params["response_format"] = "b64_json"
            response = OpenAIClient(api_key=self.api_key).images.generate(
                model=self.image_model,
                prompt=prompt,
                **extra_params,
            )
            data = response.data[0] if getattr(response, "data", None) else None
            if data is None:
                return ToolValue(content="Failed to generate image: No data received from API.")
            b64_data = getattr(data, "b64_json", None)
            if not b64_data:
                return ToolValue(content="Failed to generate image: No content received from API.")
            image = Image(
                id=str(uuid4()),
                content=base64.b64decode(b64_data),
                mime_type="image/png",
                original_prompt=prompt,
            )
            return ToolValue(content="Image generated successfully.", images=(image,))
        except Exception as exc:
            logger.exception("Failed to generate image", model=self.image_model)
            return ToolValue(content=f"Failed to generate image: {exc}")

    async def generate_speech(self, text_input: str) -> ToolValue:
        """Synthesize speech audio from text using OpenAI.

        Args:
            text_input: Text to read aloud verbatim.

        """
        if is_realtime_speech_model(self.tts_model):
            if self.tts_format not in {"wav", "pcm"}:
                return ToolValue(
                    content=(
                        f"Failed to generate speech: {self.tts_model} produces only wav or pcm audio, "
                        f"not {self.tts_format}; set text_to_speech_format to wav or pcm."
                    ),
                )
            try:
                pcm = await synthesize_realtime_speech(
                    text_input,
                    model=self.tts_model,
                    voice=self.tts_voice,
                    api_key=self.api_key,
                )
            except Exception as exc:
                logger.exception("Failed to generate realtime speech")
                return ToolValue(content=f"Failed to generate speech: {exc}")
            media_id = str(uuid4())
            audio = Audio(
                id=media_id,
                content=pcm_to_wav(pcm) if self.tts_format == "wav" else pcm,
                mime_type=f"audio/{self.tts_format}",
            )
            return ToolValue(content=f"Speech generated successfully with ID: {media_id}", audios=(audio,))

        try:
            response = await asyncio.to_thread(
                lambda: OpenAIClient(api_key=self.api_key).audio.speech.create(
                    model=self.tts_model,
                    voice=self.tts_voice,
                    input=text_input,
                    response_format=self.tts_format,  # ty: ignore[invalid-argument-type]
                ),
            )
            media_id = str(uuid4())
            audio = Audio(
                id=media_id,
                content=response.content,
                mime_type=f"audio/{self.tts_format}",
            )
            return ToolValue(content=f"Speech generated successfully with ID: {media_id}", audios=(audio,))
        except Exception as exc:
            logger.exception("Failed to generate speech")
            return ToolValue(content=f"Failed to generate speech: {exc}")
