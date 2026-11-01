"""Matrix voice-message tool for one-call TTS delivery."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, ClassVar, Literal, cast, get_args
from urllib.parse import urlparse

from openai import APIStatusError, OpenAI

from mindroom.credentials.environment import get_secret_from_env
from mindroom.custom_tools.attachment_helpers import (
    explicit_thread_root,
    resolve_context_thread_id,
    resolve_requested_room_id,
    room_access_allowed,
)
from mindroom.custom_tools.matrix_conversation_operations import MatrixMessageOperationResult, MatrixMessageOperations
from mindroom.custom_tools.matrix_helpers import check_rate_limit
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_audio_message
from mindroom.matrix.voice_message import can_prepare_voice_audio, prepare_voice_audio_bytes
from mindroom.model_defaults import LOCAL_OPENAI_API_KEY_DEFAULT, OPENAI_TTS, OPENROUTER_BASE_URL_DEFAULT
from mindroom.tool_system.authoring import Toolkit
from mindroom.voice.openai_realtime_speech import (
    RealtimeSpeechError,
    is_realtime_speech_model,
    pcm_to_wav,
    synthesize_realtime_speech,
)

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_OPUS_FILENAME = "voice-message.opus"
_DEFAULT_RESPONSE_FORMAT = "opus"
_SpeechResponseFormat = Literal["aac", "flac", "mp3", "opus", "wav"]
_ALLOWED_RESPONSE_FORMATS = frozenset(get_args(_SpeechResponseFormat))
# OpenRouter's /audio/speech endpoint only returns mp3 or pcm; mp3 is the one we can turn into a Matrix voice message.
_OPENROUTER_RESPONSE_FORMAT = "mp3"
# A Realtime model speaks PCM, which the tool wraps as WAV for transcoding into the voice note.
_REALTIME_RESPONSE_FORMAT = "wav"
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _VoiceMessagePreflight:
    """Validated inputs needed before companion or speech delivery."""

    text: str
    room_id: str
    api_key: str
    base_url: str | None
    response_format: str


def _normalize_openai_base_url(base_url: str | None) -> str | None:
    normalized = base_url.strip().rstrip("/") if isinstance(base_url, str) else ""
    if not normalized:
        return None
    if normalized.endswith("/audio/speech"):
        return normalized[: -len("/audio/speech")]
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"


def _is_openrouter_base_url(base_url: str | None) -> bool:
    if base_url is None:
        return False
    return urlparse(base_url).hostname == "openrouter.ai"


def _is_openrouter_model(model: str) -> bool:
    """OpenRouter voice models are provider-prefixed, e.g. ``hexgrad/kokoro-82m``."""
    return "/" in model


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
        explicit = self._base_url or _normalize_openai_base_url(get_secret_from_env("TTS_URL", context.runtime_paths))
        if explicit is not None:
            return explicit
        if _is_openrouter_model(self._model):
            logger.info(
                "matrix_voice_tts_routed_to_openrouter",
                model=self._model,
                reason="provider_prefixed_model_without_explicit_base_url",
            )
            return OPENROUTER_BASE_URL_DEFAULT
        return None

    def _api_key_for_context(self, context: ToolRuntimeContext, *, base_url: str | None) -> str | None:
        if self._api_key:
            return self._api_key
        if _is_openrouter_base_url(base_url):
            return get_secret_from_env("OPENROUTER_API_KEY", context.runtime_paths)
        if base_url is not None:
            return LOCAL_OPENAI_API_KEY_DEFAULT
        return get_secret_from_env("OPENAI_API_KEY", context.runtime_paths)

    def _response_format_for_target(self, base_url: str | None) -> str:
        if is_realtime_speech_model(self._model):
            return _REALTIME_RESPONSE_FORMAT
        if _is_openrouter_base_url(base_url) and self._response_format != _OPENROUTER_RESPONSE_FORMAT:
            logger.info(
                "matrix_voice_response_format_coerced",
                configured_format=self._response_format,
                effective_format=_OPENROUTER_RESPONSE_FORMAT,
                reason="openrouter_speech_supports_mp3_only",
            )
            return _OPENROUTER_RESPONSE_FORMAT
        return self._response_format

    def _generate_speech_bytes(self, *, api_key: str, base_url: str | None, text: str, response_format: str) -> bytes:
        client = OpenAI(api_key=api_key) if base_url is None else OpenAI(api_key=api_key, base_url=base_url)
        response_format = cast("_SpeechResponseFormat", response_format)
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

    async def _speech_audio(self, preflight: _VoiceMessagePreflight) -> bytes:
        if is_realtime_speech_model(self._model):
            pcm = await synthesize_realtime_speech(
                preflight.text,
                model=self._model,
                voice=self._voice,
                api_key=preflight.api_key,
                base_url=preflight.base_url,
            )
            return pcm_to_wav(pcm)
        return await asyncio.to_thread(
            self._generate_speech_bytes,
            api_key=preflight.api_key,
            base_url=preflight.base_url,
            text=preflight.text,
            response_format=preflight.response_format,
        )

    async def _send_companion_message(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        thread_id: str | None,
        companion_message: str | None,
    ) -> MatrixMessageOperationResult | None:
        companion_text = (companion_message or "").strip()
        if not companion_text:
            return None

        return await self._message_operations.dispatch_action(
            context,
            action="send",
            message=companion_text,
            attachments=[],
            room_id=room_id,
            event_id=None,
            thread_id=thread_id,
            recipient_user_id=None,
            room_mode=thread_id is None,
            new_thread=False,
            message_extras=None,
            read_limit=1,
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

    async def _preflight(  # noqa: PLR0911
        self,
        context: ToolRuntimeContext,
        *,
        text: str,
        room_id: str | None,
    ) -> _VoiceMessagePreflight | str:
        normalized_text = text.strip()
        if not normalized_text:
            return self._payload("error", message="text is required and must be non-empty.")

        if self._response_format not in _ALLOWED_RESPONSE_FORMATS:
            return self._payload(
                "error",
                message=f"response_format must be one of: {', '.join(sorted(_ALLOWED_RESPONSE_FORMATS))}.",
            )

        base_url = self._base_url_for_context(context)
        response_format = self._response_format_for_target(base_url)
        if not can_prepare_voice_audio(response_format):
            return self._payload(
                "error",
                message="Voice messages from gpt-realtime models and non-opus formats need ffmpeg and ffprobe on the backend host.",
            )

        # A blank room_id means the current room, as omitting it does.
        resolved_room_id, room_error = await resolve_requested_room_id(context, (room_id or "").strip() or None)
        if room_error is not None or resolved_room_id is None:
            return self._payload("error", message=room_error)
        if not await room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if (limit_error := self._check_rate_limit(context, resolved_room_id)) is not None:
            return self._payload(
                "error",
                room_id=resolved_room_id,
                message=limit_error,
            )

        api_key = self._api_key_for_context(context, base_url=base_url)
        if not api_key:
            missing_key_message = (
                "OPENROUTER_API_KEY is required to use an OpenRouter voice model with matrix_voice_message."
                if _is_openrouter_base_url(base_url)
                else "OPENAI_API_KEY or a local TTS base_url/TTS_URL is required for matrix_voice_message."
            )
            return self._payload(
                "error",
                room_id=resolved_room_id,
                message=missing_key_message,
            )

        return _VoiceMessagePreflight(
            text=normalized_text,
            room_id=resolved_room_id,
            api_key=api_key,
            base_url=base_url,
            response_format=response_format,
        )

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
        latest_thread_event_id = await context.conversation_reader.latest_thread_event_id(
            room_id=room_id,
            thread_id=thread_id,
            # The companion text was just sent into this thread, so it is the
            # newest event by construction. The projection would answer with
            # whatever preceded it until that echo arrives, chaining the audio
            # under the wrong message for a thread-blind client.
            known_latest_thread_event_id=companion_event_id,
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
        """Generate and send a Matrix voice message using configured text-to-speech.

        Sends one voice-note `m.audio` event to the current room/thread. `thread_id="room"` forces room scope. `companion_message` sends normal text to the same target first; `caption` only labels the audio event.

        Args:
            text (str): Required spoken content.
            room_id (str | None): Target room; defaults to the current room.
            thread_id (str | None): Explicit thread; `thread_id="room"` forces room scope.
            caption (str | None): Matrix event body; defaults to a generated filename.
            companion_message (str | None): Normal text message sent first, with mentions suppressed.

        """
        if isinstance(context := tool_context_or_error(self._payload, "Matrix voice message"), str):
            return context

        preflight = await self._preflight(context, text=text, room_id=room_id)
        if isinstance(preflight, str):
            return preflight

        if thread_id is not None and thread_id != self._ROOM_TIMELINE_SENTINEL:
            thread_id = await explicit_thread_root(context, room_id=preflight.room_id, thread_id=thread_id)
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
            audio_bytes = await self._speech_audio(preflight)
        except Exception as error:
            status_code = error.status_code if isinstance(error, (APIStatusError, RealtimeSpeechError)) else None
            error_code, error_param = (
                (error.code, error.param) if isinstance(error, RealtimeSpeechError) else (None, None)
            )
            logger.error(  # noqa: TRY400 - avoid traceback logging for provider errors that may include secrets.
                "matrix_voice_tts_generation_failed",
                room_id=preflight.room_id,
                thread_id=effective_thread_id,
                error_type=error.__class__.__name__,
                status_code=status_code,
                error_code=error_code,
                error_param=error_param,
            )
            return self._payload(
                "error",
                room_id=preflight.room_id,
                thread_id=effective_thread_id,
                companion_event_id=companion_event_id,
                message="Failed to generate speech.",
            )

        prepared_audio = await prepare_voice_audio_bytes(audio_bytes, response_format=preflight.response_format)
        if prepared_audio is None:
            logger.error(
                "matrix_voice_audio_preparation_failed",
                room_id=preflight.room_id,
                thread_id=effective_thread_id,
                response_format=preflight.response_format,
            )
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
            mimetype=prepared_audio.mimetype,
            filename=_OPUS_FILENAME,
            caption=(caption or "").strip() or None,
            duration_ms=prepared_audio.duration_ms,
            waveform=prepared_audio.waveform,
            thread_id=effective_thread_id,
            latest_thread_event_id=latest_thread_event_id,
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
