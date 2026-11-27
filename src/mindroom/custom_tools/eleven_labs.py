"""Native ElevenLabs toolkit for voices, text-to-speech, and sound effects."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

try:
    from elevenlabs import ElevenLabs
except ImportError as exc:
    _msg = "`elevenlabs` not installed. Install with: `pip install elevenlabs`"
    raise ImportError(_msg) from exc

from mindroom.llm.media import Audio
from mindroom.logging_config import get_logger
from mindroom.model_defaults import ELEVENLABS_TTS
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

logger = get_logger(__name__)

_ElevenLabsAudioOutputFormat = Literal[
    "mp3_22050_32",
    "mp3_44100_32",
    "mp3_44100_64",
    "mp3_44100_96",
    "mp3_44100_128",
    "mp3_44100_192",
    "pcm_16000",
    "pcm_22050",
    "pcm_24000",
    "pcm_44100",
    "ulaw_8000",
]


def _mime_type_for_format(output_format: str) -> str:
    if output_format.startswith("pcm"):
        return "audio/pcm"
    if output_format.startswith("ulaw"):
        return "audio/basic"
    return "audio/mpeg"


def _extension_for_format(output_format: str) -> str:
    if output_format.startswith("pcm"):
        return "wav"
    if output_format.startswith("ulaw"):
        return "ulaw"
    return "mp3"


class ElevenLabsTools(Toolkit):
    """List voices, synthesize speech, and generate sound effects with ElevenLabs."""

    def __init__(
        self,
        voice_id: str = "JBFqnCBsd6RMkjVDRZzb",
        api_key: str | None = None,
        target_directory: str | None = None,
        model_id: str = ELEVENLABS_TTS,
        output_format: _ElevenLabsAudioOutputFormat = "mp3_44100_64",
        enable_get_voices: bool = True,
        enable_generate_sound_effect: bool = True,
        enable_text_to_speech: bool = True,
        all: bool = False,  # noqa: A002
    ) -> None:
        self.api_key = api_key
        if not self.api_key:
            logger.error("ELEVEN_LABS_API_KEY not set")

        self.target_directory = target_directory
        self.voice_id = voice_id
        self.model_id = model_id
        self.output_format = output_format

        if self.target_directory:
            Path(self.target_directory).mkdir(parents=True, exist_ok=True)

        self.eleven_labs_client: Any = ElevenLabs(api_key=self.api_key)

        tools: list[Callable[..., Any]] = []
        if all or enable_get_voices:
            tools.append(self.eleven_labs_get_voices)
        if all or enable_generate_sound_effect:
            tools.append(self.eleven_labs_generate_sound_effect)
        if all or enable_text_to_speech:
            tools.append(self.eleven_labs_text_to_speech)
        super().__init__(name="eleven_labs", tools=tools)

    def _collect_audio(self, audio_generator: Iterator[bytes]) -> bytes:
        buffer = BytesIO()
        for chunk in audio_generator:
            buffer.write(chunk)
        audio_data = buffer.getvalue()
        if self.target_directory:
            extension = _extension_for_format(self.output_format)
            output_path = Path(self.target_directory) / f"{uuid4()}.{extension}"
            output_path.write_bytes(audio_data)
        return audio_data

    def eleven_labs_get_voices(self) -> str:
        """List available ElevenLabs voices with ID, name, and description."""
        try:
            voices = self.eleven_labs_client.voices.get_all()
            items = [
                {"id": voice.voice_id, "name": voice.name, "description": voice.description} for voice in voices.voices
            ]
            return json.dumps(items)
        except Exception as exc:
            logger.exception("Failed to fetch ElevenLabs voices")
            return f"Error: {exc}"

    def eleven_labs_generate_sound_effect(self, prompt: str, duration_seconds: float | None = None) -> ToolValue:
        """Generate a sound effect from a text prompt.

        Args:
            prompt: Sound effect description.
            duration_seconds: Optional duration in seconds (0.5-22).

        """
        try:
            audio_generator = self.eleven_labs_client.text_to_sound_effects.convert(
                text=prompt,
                duration_seconds=duration_seconds,
            )
            audio_data = self._collect_audio(audio_generator)
            audio = Audio(id=str(uuid4()), content=audio_data, mime_type="audio/mpeg")
            return ToolValue(content="Sound effect generated successfully", audios=(audio,))
        except Exception as exc:
            logger.exception("Failed to generate sound effect with ElevenLabs")
            return ToolValue(content=f"Error: {exc}")

    def eleven_labs_text_to_speech(self, prompt: str) -> ToolValue:
        """Convert text to speech audio using ElevenLabs.

        Args:
            prompt: Text to synthesize into speech.

        """
        try:
            audio_generator = self.eleven_labs_client.text_to_speech.convert(
                text=prompt,
                voice_id=self.voice_id,
                model_id=self.model_id,
                output_format=self.output_format,
            )
            audio_data = self._collect_audio(audio_generator)
            audio = Audio(
                id=str(uuid4()),
                content=audio_data,
                mime_type=_mime_type_for_format(self.output_format),
            )
            return ToolValue(content="Audio generated successfully", audios=(audio,))
        except Exception as exc:
            logger.exception("Failed to synthesize speech with ElevenLabs")
            return ToolValue(content=f"Error: {exc}")
