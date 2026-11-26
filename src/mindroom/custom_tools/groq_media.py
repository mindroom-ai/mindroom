"""Native Groq audio toolkit for transcription, translation, and text-to-speech."""

from __future__ import annotations

import os
from pathlib import Path  # noqa: TC003 - toolkit introspection evaluates constructor annotations.
from typing import TYPE_CHECKING, Any
from uuid import uuid4

try:
    from groq import Groq as GroqClient
except ImportError as exc:
    msg = "`groq` not installed. Install with: `pip install groq`"
    raise ImportError(msg) from exc

from mindroom.file_access import resolve_agent_file
from mindroom.llm.media import Audio
from mindroom.logging_config import get_logger
from mindroom.model_defaults import GROQ_TRANSCRIPTION, GROQ_TTS
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.models import FileAccess

logger = get_logger(__name__)


class GroqTools(Toolkit):
    """Groq Whisper transcription/translation and speech synthesis toolkit."""

    def __init__(
        self,
        api_key: str | None = None,
        transcription_model: str = GROQ_TRANSCRIPTION,
        translation_model: str = GROQ_TRANSCRIPTION,
        tts_model: str = GROQ_TTS,
        tts_voice: str = "troy",
        enable_transcribe_audio: bool = True,
        enable_translate_audio: bool = True,
        enable_generate_speech: bool = True,
        all: bool = False,  # noqa: A002
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
    ) -> None:
        resolved_key = api_key or os.getenv("GROQ_API_KEY")
        if not resolved_key:
            msg = "GROQ_API_KEY not set. Please set the GROQ_API_KEY environment variable."
            raise ValueError(msg)

        self.api_key: str = resolved_key
        self.client: Any = GroqClient(api_key=self.api_key)
        self.transcription_model = transcription_model
        self.translation_model = translation_model
        self.tts_model = tts_model
        self.tts_voice = tts_voice
        self.tts_format = "wav"
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access

        tools: list[Callable[..., Any]] = []
        if all or enable_transcribe_audio:
            tools.append(self.transcribe_audio)
        if all or enable_translate_audio:
            tools.append(self.translate_audio)
        if all or enable_generate_speech:
            tools.append(self.generate_speech)
        super().__init__(name="groq", tools=tools)

    def _request_audio(self, create: Callable[..., object], model: str, audio_source: str) -> str:
        """Send an http(s) URL directly or upload a file authorized by ``file_access``."""
        if audio_source.lower().startswith(("http://", "https://")):
            return str(create(url=audio_source, model=model, response_format="text"))
        authorized = resolve_agent_file(
            audio_source,
            workspace_root=self._workspace_root,
            file_access=self._file_access,
            field_name="audio_source",
        )
        with authorized.open() as audio_file:
            return str(create(file=(authorized.name, audio_file), model=model, response_format="text"))

    def transcribe_audio(self, audio_source: str) -> str:
        """Transcribe audio from an http(s) URL or local file using Groq Whisper.

        Args:
            audio_source: http(s) URL or local audio path (confined to workspace unless `file_access: unrestricted`).

        """
        try:
            return self._request_audio(self.client.audio.transcriptions.create, self.transcription_model, audio_source)
        except Exception as exc:
            logger.exception("Failed to transcribe audio with Groq", audio_source=audio_source)
            return f"Failed to transcribe audio source '{audio_source}' with Groq: {exc}"

    def translate_audio(self, audio_source: str) -> str:
        """Translate audio from an http(s) URL or local file into English using Groq Whisper.

        Args:
            audio_source: http(s) URL or local audio path (confined to workspace unless `file_access: unrestricted`).

        """
        try:
            return self._request_audio(self.client.audio.translations.create, self.translation_model, audio_source)
        except Exception as exc:
            logger.exception("Failed to translate audio with Groq", audio_source=audio_source)
            return f"Failed to translate audio source '{audio_source}' with Groq: {exc}"

    def generate_speech(self, text_input: str) -> ToolValue:
        """Synthesize speech audio from text using Groq TTS.

        Args:
            text_input: Text to synthesize into speech.

        """
        try:
            response = self.client.audio.speech.create(
                model=self.tts_model,
                voice=self.tts_voice,
                input=text_input,
                response_format=self.tts_format,
            )
            audio_data: bytes = response.read()
            media_id = str(uuid4())
            audio = Audio(
                id=media_id,
                content=audio_data,
                mime_type="audio/wav",
            )
            return ToolValue(content=f"Speech generated successfully with ID: {media_id}", audios=(audio,))
        except Exception as exc:
            logger.exception("Failed to generate speech with Groq")
            return ToolValue(content=f"Failed to generate speech with Groq: {exc}")
