"""Speech-to-text for one stretch of a call, through the voice-message settings ``voice.stt``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.helper_usage import record_system_usage
from mindroom.logging_config import get_logger
from mindroom.voice.handler import transcribe_speech

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SpeechTranscriber:
    """Transcribes WAV stretches with the settings read for each one, hinting the first of ``calls.transcripts.languages``."""

    config: Callable[[], Config]
    runtime_paths: RuntimePaths

    async def transcribe(self, audio: bytes, *, call_id: str, user_id: str, seconds: float) -> str | None:
        """Return what was said, recording the speech service's usage; None when it could not be transcribed."""
        config = self.config()
        languages = config.calls.transcripts.languages
        transcript = await transcribe_speech(
            audio,
            config,
            self.runtime_paths,
            mime_type="audio/wav",
            language=languages[0] if languages else None,
        )
        if transcript is None:
            return None
        if transcript.usage is not None:
            await record_system_usage(transcript.usage, runtime_paths=self.runtime_paths, kind="call_transcript")
        logger.debug("call_transcript_stretch_transcribed", call_id=call_id, user_id=user_id, seconds=seconds)
        return transcript.text
