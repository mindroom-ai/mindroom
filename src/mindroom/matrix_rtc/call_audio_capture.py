"""Opt-in PCM capture of GPT-Live calls for offline audio analysis.

Only ``MINDROOM_CALL_AUDIO_CAPTURE_DIR`` enables it. Each provider session gets
its own subdirectory with three 24 kHz mono PCM16 WAV files, each paired with
a JSONL log holding one line per frame:

- ``provider_out``: provider output deltas before the output gate.
- ``published``: frames the native ``AudioSource`` accepted after the gate,
  with its ``queued_ms`` at that moment.
- ``mic_in``: microphone frames handed to the session for the provider, with
  ``zero_filled`` marking silence the paced input loop substituted.

Every line has ``t_wall_ns`` (``time.time_ns``), ``t_mono_ns``
(``time.monotonic_ns``), ``samples``, and ``rms`` normalized to full scale.
The event loop only enqueues bytes; a writer thread owns all file I/O.
"""

from __future__ import annotations

import asyncio
import json
import math
import queue
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from livekit import rtc
    from livekit.agents import AgentSession

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_CALL_AUDIO_CAPTURE_DIR_ENV = "MINDROOM_CALL_AUDIO_CAPTURE_DIR"
_SAMPLE_RATE = 24_000

type _Stream = Literal["provider_out", "published", "mic_in"]
_STREAMS: tuple[_Stream, ...] = ("provider_out", "published", "mic_in")


def call_audio_capture_dir(runtime_paths: RuntimePaths) -> Path | None:
    """Return the capture root, or None when capture is disabled."""
    raw = (runtime_paths.env_value(_CALL_AUDIO_CAPTURE_DIR_ENV) or "").strip()
    return Path(raw).expanduser() if raw else None


@dataclass(frozen=True, slots=True)
class _CapturedAudio:
    stream: _Stream
    pcm: bytes
    t_wall_ns: int
    t_mono_ns: int
    extra: dict[str, float | bool]


def _rms(pcm: bytes) -> float:
    """Return RMS normalized to full scale, the unit LiveKit's output gate compares."""
    samples = memoryview(pcm).cast("h")
    if not samples:
        return 0.0
    return math.sqrt(math.sumprod(samples, samples) / len(samples)) / 32768


def _open_wav(path: Path) -> wave.Wave_write:
    wav = wave.open(str(path), "wb")  # noqa: SIM115 - the writer thread closes it after draining
    wav.setnchannels(1)
    wav.setsampwidth(2)
    wav.setframerate(_SAMPLE_RATE)
    return wav


class CallAudioCapture:
    """Write one call's provider, published, and microphone PCM off the event loop."""

    def __init__(self, root: Path) -> None:
        self.directory = root / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
        self.directory.mkdir(parents=True)
        self._files: dict[_Stream, tuple[wave.Wave_write, TextIO]] = {
            stream: (
                _open_wav(self.directory / f"{stream}.wav"),
                (self.directory / f"{stream}.jsonl").open("w", encoding="utf-8"),
            )
            for stream in _STREAMS
        }
        self._queue: queue.SimpleQueue[_CapturedAudio | None] = queue.SimpleQueue()
        self._closed = False
        self._thread = threading.Thread(target=self._write, name="call-audio-capture", daemon=True)
        self._thread.start()
        logger.info("call_audio_capture_started", path=str(self.directory))

    def provider_out(self, pcm: bytes) -> None:
        """Record one decoded provider output delta."""
        self._put("provider_out", pcm, {})

    def published(self, frame: rtc.AudioFrame, queued_ms: float) -> None:
        """Record one frame the native source accepted, with its queued duration afterwards."""
        self._put("published", frame.data.tobytes(), {"queued_ms": round(queued_ms, 1)})

    def mic_in(self, frame: rtc.AudioFrame, zero_filled: bool) -> None:
        """Record one microphone frame handed to the session."""
        self._put("mic_in", frame.data.tobytes(), {"zero_filled": zero_filled})

    def _put(self, stream: _Stream, pcm: bytes, extra: dict[str, float | bool]) -> None:
        if not self._closed:
            self._queue.put(_CapturedAudio(stream, pcm, time.time_ns(), time.monotonic_ns(), extra))

    def _write(self) -> None:
        while (audio := self._queue.get()) is not None:
            wav, frames = self._files[audio.stream]
            wav.writeframes(audio.pcm)
            line = {
                "t_wall_ns": audio.t_wall_ns,
                "t_mono_ns": audio.t_mono_ns,
                "samples": len(audio.pcm) // 2,
                "rms": round(_rms(audio.pcm), 6),
                **audio.extra,
            }
            frames.write(json.dumps(line) + "\n")
        for wav, frames in self._files.values():
            wav.close()
            frames.close()

    async def aclose(self) -> None:
        """Stop accepting frames and wait until everything queued is on disk."""
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        await asyncio.to_thread(self._thread.join)
        logger.info("call_audio_capture_closed", path=str(self.directory))


def tap_published_audio(session: AgentSession, capture: CallAudioCapture) -> None:
    """Record each frame when RoomIO's native ``AudioSource`` accepts it.

    LiveKit exposes no public hook here. ``AudioOutput`` wrappers chained with
    ``next_in_chain`` see frames before RoomIO's own unbounded buffer, so they
    cannot show when audio actually enters the published track. Wrapping
    ``capture_frame`` on this one private source instance observes accepted
    frames and the source's ``queued_duration`` without altering either.
    """
    from livekit.agents.voice.room_io._output import _ParticipantAudioOutput  # noqa: PLC0415

    output = session.output.audio
    while output is not None and output.next_in_chain is not None:
        output = output.next_in_chain
    if not isinstance(output, _ParticipantAudioOutput):
        logger.warning("call_audio_capture_published_unavailable", output=type(output).__name__)
        return
    source = output._audio_source
    capture_frame = source.capture_frame

    async def capture_and_record(frame: rtc.AudioFrame) -> None:
        await capture_frame(frame)
        capture.published(frame, source.queued_duration * 1000)

    source.capture_frame = capture_and_record  # ty: ignore[invalid-assignment]
