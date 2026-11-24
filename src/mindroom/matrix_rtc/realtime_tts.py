"""LiveKit text-to-speech for cascaded calls that speaks through the OpenAI Realtime API.

LiveKit's OpenAI TTS speaks only through ``/v1/audio/speech``, which does not serve ``gpt-realtime`` models.
"""

from __future__ import annotations

from uuid import uuid4

from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, APIError, APIStatusError, tts

from mindroom.voice.openai_realtime_speech import (
    REALTIME_SPEECH_SAMPLE_RATE,
    RealtimeSpeechError,
    stream_realtime_speech,
)

__all__ = ["RealtimeTTS"]


class RealtimeTTS(tts.TTS):
    """Speak each segment of a call reply with a ``gpt-realtime`` model."""

    def __init__(self, *, model: str, api_key: str, base_url: str | None = None, voice: str = "alloy") -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=REALTIME_SPEECH_SAMPLE_RATE,
            num_channels=1,
        )
        self._model = model
        self._api_key = api_key
        self._base_url = base_url
        self._voice = voice

    @property
    def model(self) -> str:
        """Return the Realtime model that speaks."""
        return self._model

    @property
    def provider(self) -> str:
        """Return the speech provider's name."""
        return "openai"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        """Return the stream that speaks one segment."""
        return _RealtimeChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class _RealtimeChunkedStream(tts.ChunkedStream):
    def __init__(self, *, tts: RealtimeTTS, input_text: str, conn_options: APIConnectOptions) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._realtime_tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=uuid4().hex,
            sample_rate=REALTIME_SPEECH_SAMPLE_RATE,
            num_channels=1,
            mime_type="audio/pcm",
        )
        try:
            async for chunk in stream_realtime_speech(
                self.input_text,
                model=self._realtime_tts.model,
                voice=self._realtime_tts._voice,
                api_key=self._realtime_tts._api_key,
                base_url=self._realtime_tts._base_url,
            ):
                output_emitter.push(chunk)
        except RealtimeSpeechError as error:
            # A refused key or voice fails every attempt, and played speech cannot be taken back, so a failed segment is
            # not retried; a refusal keeps its HTTP status, which the call's error notice explains.
            if error.status_code is not None:
                raise APIStatusError(str(error), status_code=error.status_code, retryable=False) from error
            raise APIError(str(error), retryable=False) from error
        output_emitter.flush()
