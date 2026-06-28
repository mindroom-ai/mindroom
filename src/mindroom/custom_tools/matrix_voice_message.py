"""Matrix voice-message tool for one-call TTS delivery."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from threading import Lock
from typing import ClassVar, Literal

from agno.tools import Toolkit
from openai import OpenAI

from mindroom.credentials_sync import get_secret_from_env
from mindroom.custom_tools.attachment_helpers import resolve_context_thread_id, room_access_allowed
from mindroom.custom_tools.matrix_helpers import check_rate_limit
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.matrix.client_delivery import send_audio_message
from mindroom.model_defaults import OPENAI_TTS
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context

_SpeechResponseFormat = Literal["mp3", "opus", "aac", "flac", "wav", "pcm"]


class MatrixVoiceMessageTools(Toolkit):
    """Native Matrix voice-message action for general agents."""

    _rate_limit_lock: ClassVar[Lock] = Lock()
    _recent_actions: ClassVar[dict[tuple[str, str, str], deque[float]]] = defaultdict(deque)
    _RATE_LIMIT_WINDOW_SECONDS: ClassVar[float] = 30.0
    _RATE_LIMIT_MAX_ACTIONS: ClassVar[int] = 6
    _ROOM_TIMELINE_SENTINEL: ClassVar[str] = "room"
    _MIMETYPE_BY_FORMAT: ClassVar[dict[str, str]] = {
        "mp3": "audio/mpeg",
        "opus": "audio/ogg",
        "aac": "audio/aac",
        "flac": "audio/flac",
        "wav": "audio/wav",
        "pcm": "audio/pcm",
    }

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = OPENAI_TTS,
        voice: str = "alloy",
        response_format: _SpeechResponseFormat = "mp3",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._voice = voice
        self._response_format = response_format
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

    @classmethod
    def _mimetype_for_response_format(cls, response_format: str) -> str:
        normalized_format = response_format.strip().lower()
        return cls._MIMETYPE_BY_FORMAT.get(normalized_format, f"audio/{normalized_format or 'mpeg'}")

    @staticmethod
    def _filename_for_response_format(response_format: str) -> str:
        normalized_format = response_format.strip().lower()
        extension = normalized_format or "mp3"
        return f"voice-message.{extension}"

    def _api_key_for_context(self, context: ToolRuntimeContext) -> str | None:
        return self._api_key or get_secret_from_env("OPENAI_API_KEY", context.runtime_paths)

    def _generate_speech_bytes(self, *, api_key: str, text: str) -> bytes:
        response = OpenAI(api_key=api_key).audio.speech.create(
            model=self._model,
            voice=self._voice,
            input=text,
            response_format=self._response_format,
        )
        audio_content = response.content
        if not isinstance(audio_content, bytes):
            msg = "OpenAI speech response did not include audio bytes."
            raise TypeError(msg)
        return audio_content

    async def matrix_voice_message(  # noqa: PLR0911
        self,
        text: str,
        room_id: str | None = None,
        thread_id: str | None = None,
        caption: str | None = None,
    ) -> str:
        """Generate and send a Matrix voice message from text using OpenAI text-to-speech.

        The tool sends one `m.audio` Matrix event with voice-message metadata.
        It defaults to the current room and current thread.
        Pass `thread_id="room"` to force a room-level voice message instead of inheriting the active thread.

        Args:
            text (str): Required spoken content to synthesize into the voice message.
            room_id (str | None): Optional target room ID or alias; defaults to the current room context when omitted.
            thread_id (str | None): Optional explicit thread target; `thread_id="room"` forces room-level scope instead of inheriting the current thread.
            caption (str | None): Optional Matrix event body shown beside the audio. If omitted, the event body is a short generated audio filename.

        """
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        normalized_text = text.strip() if isinstance(text, str) else ""
        if not normalized_text:
            return self._payload("error", message="text is required and must be non-empty.")

        resolved_room_id = room_id or context.room_id
        if not room_access_allowed(context, resolved_room_id):
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

        api_key = self._api_key_for_context(context)
        if not api_key:
            return self._payload(
                "error",
                room_id=resolved_room_id,
                message="OPENAI_API_KEY is required for matrix_voice_message.",
            )

        effective_thread_id = resolve_context_thread_id(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
            room_timeline_sentinel=self._ROOM_TIMELINE_SENTINEL,
        )
        latest_thread_event_id = None
        if effective_thread_id is not None:
            latest_thread_event_id = await context.conversation_cache.get_latest_thread_event_id_if_needed(
                resolved_room_id,
                effective_thread_id,
                caller_label="matrix_voice_message_tool",
            )

        try:
            audio_bytes = await asyncio.to_thread(
                self._generate_speech_bytes,
                api_key=api_key,
                text=normalized_text,
            )
        except Exception:
            return self._payload(
                "error",
                room_id=resolved_room_id,
                thread_id=effective_thread_id,
                message="Failed to generate speech.",
            )

        event_id = await send_audio_message(
            context.client,
            resolved_room_id,
            audio_bytes,
            config=context.config,
            mimetype=self._mimetype_for_response_format(self._response_format),
            filename=self._filename_for_response_format(self._response_format),
            caption=caption.strip() if isinstance(caption, str) and caption.strip() else None,
            thread_id=effective_thread_id,
            latest_thread_event_id=latest_thread_event_id,
            conversation_cache=context.conversation_cache,
        )
        if event_id is None:
            return self._payload(
                "error",
                room_id=resolved_room_id,
                thread_id=effective_thread_id,
                message="Failed to send voice message to Matrix.",
            )

        return self._payload(
            "ok",
            room_id=resolved_room_id,
            thread_id=effective_thread_id,
            event_id=event_id,
        )
