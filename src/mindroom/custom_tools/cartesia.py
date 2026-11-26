"""Native Cartesia voice AI toolkit for TTS, voice listing, and voice localization."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

try:
    import cartesia
except ImportError as exc:
    _msg = "`cartesia` not installed. Install with: `pip install cartesia`"
    raise ImportError(_msg) from exc

from mindroom.llm.media import Audio
from mindroom.logging_config import get_logger
from mindroom.model_defaults import CARTESIA_TTS
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_SpeakerGender = Literal["male", "female"]


class CartesiaTools(Toolkit):
    """Cartesia text-to-speech, voice listing, and voice localization toolkit."""

    def __init__(
        self,
        api_key: str | None = None,
        model_id: str = CARTESIA_TTS,
        default_voice_id: str = "78ab82d5-25be-4f7d-82b3-7ad64e5b85b2",
        enable_text_to_speech: bool = True,
        enable_list_voices: bool = True,
        enable_localize_voice: bool = False,
        all: bool = False,  # noqa: A002
    ) -> None:
        resolved_key = api_key.strip() if isinstance(api_key, str) else ""
        if not resolved_key:
            msg = "CARTESIA_API_KEY not set. Please set the CARTESIA_API_KEY environment variable."
            raise ValueError(msg)

        self.api_key: str = resolved_key
        self.client: Any = cartesia.Cartesia(api_key=self.api_key)
        self.model_id = model_id
        self.default_voice_id = default_voice_id

        tools: list[Callable[..., Any]] = []
        if all or enable_localize_voice:
            tools.append(self.localize_voice)
        if all or enable_text_to_speech:
            tools.append(self.text_to_speech)
        if all or enable_list_voices:
            tools.append(self.list_voices)
        super().__init__(name="cartesia", tools=tools)

    def list_voices(self) -> str:
        """List available Cartesia voices (first page) as JSON."""
        try:
            voices = self.client.voices.list()
            items = voices.data if voices is not None else ()
            result = [
                {
                    "id": getattr(voice, "id", None),
                    "name": getattr(voice, "name", None),
                    "description": getattr(voice, "description", None),
                    "language": getattr(voice, "language", None),
                }
                for voice in items
                if getattr(voice, "id", None)
            ]
            return json.dumps(result)
        except Exception as exc:
            logger.exception("Error listing Cartesia voices")
            return json.dumps({"error": str(exc), "detail": "Error occurred in list_voices function."})

    def localize_voice(
        self,
        name: str,
        description: str,
        language: str,
        original_speaker_gender: _SpeakerGender,
        accent: str,
        voice_id: str | None = None,
    ) -> str:
        """Create a localized Cartesia voice in another language.

        Args:
            name: Name for the new localized voice.
            description: Voice description.
            language: Target language code (e.g. `fr`, `es`).
            original_speaker_gender: Gender of the base voice speaker.
            accent: Cartesia catalog accent ID (e.g. `parisian`, `southern-us`, `general-american`).
            voice_id: Base voice ID; defaults to the configured `default_voice_id`.

        """
        base_voice_id = voice_id or self.default_voice_id
        try:
            result = self.client.voices.localize(
                accent=accent,
                voice_id=base_voice_id,
                name=name,
                description=description,
                language=language,
                original_speaker_gender=original_speaker_gender,
            )
            if isinstance(result, dict):
                return json.dumps(result)
            return result.model_dump_json()
        except Exception as exc:
            logger.exception("Error localizing Cartesia voice")
            return json.dumps({"error": str(exc), "type": type(exc).__name__})

    def text_to_speech(self, transcript: str, voice_id: str | None = None) -> ToolValue:
        """Synthesize MP3 speech audio from text using Cartesia.

        Args:
            transcript: Text to synthesize into speech.
            voice_id: Optional voice ID; defaults to the configured `default_voice_id`.

        """
        effective_voice_id = voice_id or self.default_voice_id
        try:
            audio_response = self.client.tts.generate(
                model_id=self.model_id,
                transcript=transcript,
                voice={"mode": "id", "id": effective_voice_id},
                output_format={
                    "container": "mp3",
                    "sample_rate": 44100,
                    "bit_rate": 128000,
                },
            )
            audio_data = audio_response.read()
            audio = Audio(id=str(uuid4()), content=audio_data, mime_type="audio/mpeg")
            return ToolValue(content="Audio generated and attached successfully.", audios=(audio,))
        except Exception as exc:
            logger.exception("Error generating Cartesia speech")
            return ToolValue(content=f"Error generating speech: {exc}")
