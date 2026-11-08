"""The duration of an uploaded stretch of speech, read from its WAV header."""

from __future__ import annotations

import io
import wave

_SAMPLE_WIDTH_BYTES = 2


class WavError(ValueError):
    """The audio is not a complete 16-bit PCM mono WAV."""


def wav_seconds(audio: bytes) -> float:
    """Return the duration a 16-bit PCM mono WAV's header declares, refusing other audio and a cut-off body."""
    try:
        with wave.open(io.BytesIO(audio), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            frame_rate = reader.getframerate()
            frames = reader.getnframes()
            body = reader.readframes(frames)
    except (EOFError, wave.Error) as error:
        msg = "The audio is not a WAV with PCM samples."
        raise WavError(msg) from error
    if sample_width != _SAMPLE_WIDTH_BYTES:
        msg = f"The audio has {sample_width * 8}-bit samples; it must have 16-bit samples."
        raise WavError(msg)
    if channels != 1:
        msg = f"The audio has {channels} channels; it must be mono."
        raise WavError(msg)
    if frame_rate <= 0:
        msg = "The audio's header has no sample rate."
        raise WavError(msg)
    if len(body) < frames * _SAMPLE_WIDTH_BYTES:
        msg = "The audio is shorter than its header says."
        raise WavError(msg)
    return frames / frame_rate
