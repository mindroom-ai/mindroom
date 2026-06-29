"""Matrix voice-message tool for one-call TTS delivery."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from typing import ClassVar, Literal, cast

from agno.tools import Toolkit
from openai import APIStatusError, OpenAI

from mindroom.credentials_sync import get_secret_from_env
from mindroom.custom_tools.attachment_helpers import (
    resolve_context_thread_id,
    resolve_optional_room_id,
    room_access_allowed,
)
from mindroom.custom_tools.matrix_conversation_operations import MatrixMessageOperationResult, MatrixMessageOperations
from mindroom.custom_tools.matrix_helpers import check_rate_limit
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import prepare_voice_audio_bytes, send_audio_message
from mindroom.model_defaults import OPENAI_TTS
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context

_OPUS_FILENAME = "voice-message.opus"
_DEFAULT_RESPONSE_FORMAT = "opus"
_LOCAL_TTS_API_KEY = "sk-no-key-required"
_SpeechResponseFormat = Literal["mp3", "opus", "aac", "flac", "wav", "pcm"]
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _VoiceMessagePreflight:
    """Validated inputs needed before companion or speech delivery."""

    text: str
    room_id: str
    api_key: str
    base_url: str | None


def _normalize_openai_base_url(base_url: str | None) -> str | None:
    normalized = base_url.strip().rstrip("/") if isinstance(base_url, str) else ""
    if not normalized:
        return None
    if normalized.endswith("/v1/audio/speech"):
        return normalized[: -len("/audio/speech")]
    if normalized.endswith("/audio/speech"):
        return normalized[: -len("/audio/speech")]
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def _input_validation_error(
    text: object,
    room_id: object,
    thread_id: object,
    caption: object,
    companion_message: object,
) -> str | None:
    if not isinstance(text, str):
        return "text must be a string."
    if room_id is not None and not isinstance(room_id, str):
        return "room_id must be a string."
    if thread_id is not None and not isinstance(thread_id, str):
        return "thread_id must be a string."
    if caption is not None and not isinstance(caption, str):
        return "caption must be a string."
    if companion_message is not None and not isinstance(companion_message, str):
        return "companion_message must be a string."
    return None


class MatrixVoiceMessageTools(Toolkit):
    """Native Matrix voice-message action for general agents."""

    _rate_limit_lock: ClassVar[Lock] = Lock()
    _recent_actions: ClassVar[dict[tuple[str, str, str], deque[float]]] = defaultdict(deque)
    _RATE_LIMIT_WINDOW_SECONDS: ClassVar[float] = 30.0
    _RATE_LIMIT_MAX_ACTIONS: ClassVar[int] = 6
    _ROOM_TIMELINE_SENTINEL: ClassVar[str] = "room"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = OPENAI_TTS,
        base_url: str | None = None,
        voice: str = "alloy",
        response_format: str = _DEFAULT_RESPONSE_FORMAT,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = _normalize_openai_base_url(base_url)
        self._voice = voice
        self._response_format = response_format
        self._message_operations = MatrixMessageOperations()
        super().__init__(
            name="matrix_voice_message",
            tools=[self.matrix_voice_message],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("matrix_voice_message", status, **kwargs)

    @classmethod
    def _context_error(cls) -> str:
        return cls._payload(
            "error",
            message="Matrix voice message tool context is unavailable in this runtime path.",
        )

    @classmethod
    def _check_rate_limit(cls, context: ToolRuntimeContext, room_id: str) -> str | None:
        return check_rate_limit(
            lock=cls._rate_limit_lock,
            recent_actions=cls._recent_actions,
            window_seconds=cls._RATE_LIMIT_WINDOW_SECONDS,
            max_actions=cls._RATE_LIMIT_MAX_ACTIONS,
            tool_name="matrix_voice_message",
            context=context,
            room_id=room_id,
        )

    def _base_url_for_context(self, context: ToolRuntimeContext) -> str | None:
        return self._base_url or _normalize_openai_base_url(get_secret_from_env("TTS_URL", context.runtime_paths))

    def _api_key_for_context(self, context: ToolRuntimeContext, *, base_url: str | None) -> str | None:
        if self._api_key:
            return self._api_key
        if base_url is not None:
            return _LOCAL_TTS_API_KEY
        return get_secret_from_env("OPENAI_API_KEY", context.runtime_paths)

    def _generate_speech_bytes(self, *, api_key: str, base_url: str | None, text: str) -> bytes:
        client = OpenAI(api_key=api_key) if base_url is None else OpenAI(api_key=api_key, base_url=base_url)
        response_format = cast("_SpeechResponseFormat", self._response_format)
        response = client.audio.speech.create(
            model=self._model,
            voice=self._voice,
            input=text,
            response_format=response_format,
        )
        audio_content = response.content
        if not isinstance(audio_content, bytes):
            msg = "Speech response did not include audio bytes."
            raise TypeError(msg)
        return audio_content

    async def _send_companion_message(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        thread_id: str | None,
        companion_message: str | None,
    ) -> MatrixMessageOperationResult | None:
        companion_text = companion_message.strip() if isinstance(companion_message, str) else ""
        if not companion_text:
            return None

        return await self._message_operations.dispatch_action(
            context,
            action="thread-reply" if thread_id is not None else "send",
            message=companion_text,
            attachment_ids=[],
            attachment_file_paths=[],
            room_id=room_id,
            target=None,
            thread_id=thread_id,
            ignore_mentions=True,
            message_extras=None,
            read_limit=1,
            page_token=None,
            room_timeline_sentinel=self._ROOM_TIMELINE_SENTINEL,
        )

    def _companion_event_id_or_error(
        self,
        companion_result: MatrixMessageOperationResult | None,
        *,
        room_id: str,
        thread_id: str | None,
    ) -> tuple[str | None, str | None]:
        if companion_result is None:
            return None, None

        companion_event_id = companion_result.fields.get("event_id")
        if companion_result.status == "ok" and isinstance(companion_event_id, str):
            return companion_event_id, None

        return None, self._payload(
            "error",
            room_id=room_id,
            thread_id=thread_id,
            message="Failed to send companion message to Matrix.",
        )

    def _preflight(
        self,
        context: ToolRuntimeContext,
        *,
        text: str,
        room_id: str | None,
    ) -> tuple[_VoiceMessagePreflight | None, str | None]:
        normalized_text = text.strip()
        if not normalized_text:
            return None, self._payload("error", message="text is required and must be non-empty.")

        resolved_room_id = resolve_optional_room_id(context, room_id)
        if not room_access_allowed(context, resolved_room_id):
            return None, self._payload(
                "error",
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if (limit_error := self._check_rate_limit(context, resolved_room_id)) is not None:
            return None, self._payload(
                "error",
                room_id=resolved_room_id,
                message=limit_error,
            )

        base_url = self._base_url_for_context(context)
        api_key = self._api_key_for_context(context, base_url=base_url)
        if not api_key:
            return None, self._payload(
                "error",
                room_id=resolved_room_id,
                message="OPENAI_API_KEY or a local TTS base_url/TTS_URL is required for matrix_voice_message.",
            )

        return _VoiceMessagePreflight(
            text=normalized_text,
            room_id=resolved_room_id,
            api_key=api_key,
            base_url=base_url,
        ), None

    async def _latest_thread_event_id_or_error(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        thread_id: str | None,
        companion_event_id: str | None,
    ) -> tuple[str | None, str | None]:
        if thread_id is None:
            return None, None
        latest_thread_event_id = await context.conversation_cache.get_latest_thread_event_id_if_needed(
            room_id,
            thread_id,
            caller_label="matrix_voice_message_tool",
        )
        if latest_thread_event_id is not None:
            return latest_thread_event_id, None
        logger.error(
            "matrix_voice_thread_fallback_missing",
            room_id=room_id,
            thread_id=thread_id,
        )
        return None, self._payload(
            "error",
            room_id=room_id,
            thread_id=thread_id,
            companion_event_id=companion_event_id,
            message="Failed to resolve Matrix thread fallback for voice message.",
        )

    async def matrix_voice_message(  # noqa: PLR0911
        self,
        text: str,
        room_id: str | None = None,
        thread_id: str | None = None,
        caption: str | None = None,
        companion_message: str | None = None,
    ) -> str:
        """Generate and send a Matrix voice message from text using configured text-to-speech.

        The tool sends one `m.audio` Matrix event with voice-message metadata.
        It defaults to the current room and current thread.
        Pass `thread_id="room"` to force a room-level voice message instead of inheriting the active thread.
        Pass `companion_message` to send a normal Matrix text event to the same room/thread before the voice event.
        `caption` is only the audio event body; it is not a separate timeline message.

        Args:
            text (str): Required spoken content to synthesize into the voice message.
            room_id (str | None): Optional target room ID, full alias, or managed room key; defaults to the current room context when omitted.
            thread_id (str | None): Optional explicit thread target; `thread_id="room"` forces room-level scope instead of inheriting the current thread.
            caption (str | None): Optional Matrix event body shown beside the audio. If omitted, the event body is a short generated audio filename.
            companion_message (str | None): Optional normal text message to send to the same target before the voice message. Mentions are suppressed like `matrix_message` default text sends.

        """
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        validation_error = _input_validation_error(text, room_id, thread_id, caption, companion_message)
        if validation_error is not None:
            return self._payload("error", message=validation_error)

        preflight, preflight_error = self._preflight(context, text=text, room_id=room_id)
        if preflight_error is not None or preflight is None:
            return preflight_error or self._context_error()

        effective_thread_id = resolve_context_thread_id(
            context,
            room_id=preflight.room_id,
            thread_id=thread_id,
            room_timeline_sentinel=self._ROOM_TIMELINE_SENTINEL,
        )
        companion_result = await self._send_companion_message(
            context,
            room_id=preflight.room_id,
            thread_id=effective_thread_id,
            companion_message=companion_message,
        )
        companion_event_id, companion_error = self._companion_event_id_or_error(
            companion_result,
            room_id=preflight.room_id,
            thread_id=effective_thread_id,
        )
        if companion_error is not None:
            return companion_error

        latest_thread_event_id, thread_error = await self._latest_thread_event_id_or_error(
            context,
            room_id=preflight.room_id,
            thread_id=effective_thread_id,
            companion_event_id=companion_event_id,
        )
        if thread_error is not None:
            return thread_error

        try:
            audio_bytes = await asyncio.to_thread(
                self._generate_speech_bytes,
                api_key=preflight.api_key,
                base_url=preflight.base_url,
                text=preflight.text,
            )
        except Exception as error:
            status_code = error.status_code if isinstance(error, APIStatusError) else None
            logger.error(  # noqa: TRY400 - avoid traceback logging for provider errors that may include secrets.
                "matrix_voice_tts_generation_failed",
                room_id=preflight.room_id,
                thread_id=effective_thread_id,
                error_type=error.__class__.__name__,
                status_code=status_code,
            )
            return self._payload(
                "error",
                room_id=preflight.room_id,
                thread_id=effective_thread_id,
                companion_event_id=companion_event_id,
                message="Failed to generate speech.",
            )

        prepared_audio = await prepare_voice_audio_bytes(audio_bytes, response_format=self._response_format)
        if prepared_audio is None:
            return self._payload(
                "error",
                room_id=preflight.room_id,
                thread_id=effective_thread_id,
                companion_event_id=companion_event_id,
                message="Failed to prepare generated audio as a Matrix voice message.",
            )
        event_id = await send_audio_message(
            context.client,
            preflight.room_id,
            prepared_audio.audio_bytes,
            config=context.config,
            mimetype=prepared_audio.mimetype,
            filename=_OPUS_FILENAME,
            caption=caption.strip() if isinstance(caption, str) and caption.strip() else None,
            duration_ms=prepared_audio.duration_ms,
            waveform=prepared_audio.waveform,
            thread_id=effective_thread_id,
            latest_thread_event_id=latest_thread_event_id,
            conversation_cache=context.conversation_cache,
        )
        if event_id is None:
            return self._payload(
                "error",
                room_id=preflight.room_id,
                thread_id=effective_thread_id,
                companion_event_id=companion_event_id,
                message="Failed to send voice message to Matrix.",
            )

        return self._payload(
            "ok",
            room_id=preflight.room_id,
            thread_id=effective_thread_id,
            event_id=event_id,
            companion_event_id=companion_event_id,
        )
