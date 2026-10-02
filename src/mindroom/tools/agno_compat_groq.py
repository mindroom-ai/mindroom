"""Groq audio toolkit whose local audio sources follow the agent's ``file_access``."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - toolkit introspection evaluates constructor annotations.
from typing import TYPE_CHECKING, Any, override

from agno.tools.models.groq import GroqTools
from agno.utils.log import log_error

from mindroom.file_access import resolve_agent_file

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.models import FileAccess


class MindRoomGroqTools(GroqTools):
    """Send http(s) URLs to Groq and read other sources only where ``file_access`` allows, through no-follow descriptors."""

    def __init__(
        self,
        api_key: str | None = None,
        transcription_model: str = "whisper-large-v3",
        translation_model: str = "whisper-large-v3",
        tts_model: str = "playai-tts",
        tts_voice: str = "Chip-PlayAI",
        enable_transcribe_audio: bool = True,
        enable_translate_audio: bool = True,
        enable_generate_speech: bool = True,
        all: bool = False,  # noqa: A002 - upstream option name
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        super().__init__(
            api_key=api_key,
            transcription_model=transcription_model,
            translation_model=translation_model,
            tts_model=tts_model,
            tts_voice=tts_voice,
            enable_transcribe_audio=enable_transcribe_audio,
            enable_translate_audio=enable_translate_audio,
            enable_generate_speech=enable_generate_speech,
            all=all,
            **kwargs,
        )

    # AGNO_COMPAT: GroqTools opens any existing model-chosen path by name before treating the source as a URL.
    # Reason: Agno 3.0.9 reads the whole file whenever Path(audio_source).exists() in whichever process
    # runs the toolkit, so a prompt could upload any file that process can read, or read a device
    # without end, whatever the agent's file_access.
    # Upstream issue: Tracking gap; upstream tracking has not been verified.
    # Upstream PR: None identified.
    # Remove when: GroqTools accepts a caller-supplied file opener for local sources; retain sending only
    # http(s) URLs as URLs and resolving other sources under file_access through no-follow descriptors.
    # Coverage: tests/test_file_access_contract.py::test_outside_files_follow_file_access and
    # tests/test_file_access_contract.py::test_file_swapped_for_link_after_the_check_is_refused.
    def _request(self, create: Callable[..., object], model: str, audio_source: str) -> str:
        """Send one http(s) URL, or one authorized local file, to a Groq audio endpoint."""
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

    @override
    def transcribe_audio(self, audio_source: str) -> str:
        """Transcribe audio file or URL using Groq's Whisper API.

        Args:
            audio_source: An http(s) URL, or a local file path; with ``file_access: workspace`` the file must be inside the agent workspace.

        Returns:
            str: Transcribed text

        """
        try:
            return self._request(self.client.audio.transcriptions.create, self.transcription_model, audio_source)
        except Exception as e:
            log_error(f"Failed to transcribe audio source '{audio_source}' with Groq: {e}")
            return f"Failed to transcribe audio source '{audio_source}' with Groq: {e}"

    @override
    def translate_audio(self, audio_source: str) -> str:
        """Translate audio file or URL to English using Groq's Whisper API.

        Args:
            audio_source: An http(s) URL, or a local file path; with ``file_access: workspace`` the file must be inside the agent workspace.

        Returns:
            str: Translated English text

        """
        try:
            return self._request(self.client.audio.translations.create, self.translation_model, audio_source)
        except Exception as e:
            log_error(f"Failed to translate audio source '{audio_source}' with Groq: {e}")
            return f"Failed to translate audio source '{audio_source}' with Groq: {e}"
