"""OpenAI media toolkit whose transcription input follows the agent's ``file_access``."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - toolkit introspection evaluates constructor annotations.
from typing import TYPE_CHECKING, Any, Literal, override

from agno.tools.openai import OpenAIClient, OpenAITools, OpenAITTSFormat, OpenAITTSModel, OpenAIVoice
from agno.utils.log import log_error

from mindroom.file_access import resolve_agent_file

if TYPE_CHECKING:
    from mindroom.config.models import FileAccess


class MindRoomOpenAITools(OpenAITools):
    """Read ``transcribe_audio`` files only where the agent's ``file_access`` allows, through no-follow descriptors."""

    def __init__(
        self,
        api_key: str | None = None,
        enable_transcription: bool = True,
        enable_image_generation: bool = True,
        enable_speech_generation: bool = True,
        all: bool = False,  # noqa: A002 - upstream option name
        transcription_model: str = "whisper-1",
        text_to_speech_voice: OpenAIVoice = "alloy",
        text_to_speech_model: OpenAITTSModel = "tts-1",
        text_to_speech_format: OpenAITTSFormat = "mp3",
        image_model: str | None = "dall-e-3",
        image_quality: str | None = None,
        image_size: Literal["256x256", "512x512", "1024x1024", "1792x1024", "1024x1792"] | None = None,
        image_style: Literal["vivid", "natural"] | None = None,
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        super().__init__(
            api_key=api_key,
            enable_transcription=enable_transcription,
            enable_image_generation=enable_image_generation,
            enable_speech_generation=enable_speech_generation,
            all=all,
            transcription_model=transcription_model,
            text_to_speech_voice=text_to_speech_voice,
            text_to_speech_model=text_to_speech_model,
            text_to_speech_format=text_to_speech_format,
            image_model=image_model,
            image_quality=image_quality,
            image_size=image_size,
            image_style=image_style,
            **kwargs,
        )

    # AGNO_COMPAT: OpenAITools.transcribe_audio opens the model-chosen audio path by name.
    # Reason: Agno 3.0.9 passes audio_path to open() in whichever process runs the toolkit, so a
    # prompt could upload any file that process can read, such as MindRoom's config or credentials,
    # or block on a FIFO, whatever the agent's file_access.
    # Upstream issue: Tracking gap; upstream tracking has not been verified.
    # Upstream PR: None identified.
    # Remove when: OpenAITools accepts a caller-supplied file opener or file object for transcription;
    # retain resolution under file_access and the no-follow, non-blocking open.
    # Coverage: tests/test_file_access_contract.py::test_outside_files_follow_file_access and
    # tests/test_file_access_contract.py::test_file_swapped_for_link_after_the_check_is_refused.
    @override
    def transcribe_audio(self, audio_path: str) -> str:
        """Transcribe audio file using OpenAI's Whisper API.

        Args:
            audio_path: Path to the audio file; with ``file_access: workspace`` it must be inside the agent workspace

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
        except Exception as e:
            log_error(f"Failed to transcribe audio: {e}")
            return f"Failed to transcribe audio: {e}"
        return transcript
