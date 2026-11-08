"""Tests for reading an uploaded stretch of speech's duration from its WAV header."""

from __future__ import annotations

import io
import struct
import wave

import pytest

from mindroom.call_transcripts.wav import WavError, wav_seconds


def _wav(*, seconds: float, rate: int = 16_000, channels: int = 1, sample_width: int = 2) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(rate)
        writer.writeframes(b"\x00" * (int(seconds * rate) * channels * sample_width))
    return buffer.getvalue()


def _crafted_wav(*, format_tag: int, rate: int, bits: int) -> bytes:
    """Build a one-second mono WAV at 16 kHz whose header the standard library's writer cannot produce."""
    data = b"\x00" * (16_000 * bits // 8)
    fmt = struct.pack("<HHIIHH", format_tag, 1, rate, rate * bits // 8, bits // 8, bits)
    return (
        b"RIFF"
        + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(data))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<I", len(fmt))
        + fmt
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )


@pytest.mark.parametrize(
    ("audio", "seconds"),
    [
        (_wav(seconds=1.5), 1.5),
        (_wav(seconds=2, rate=48_000), 2.0),
        (_wav(seconds=30), 30.0),
        (_wav(seconds=0), 0.0),
    ],
    ids=["16kHz", "48kHz", "30s", "empty-body"],
)
def test_wav_seconds_reads_the_duration_from_the_header(audio: bytes, seconds: float) -> None:
    """A 16-bit PCM mono WAV's duration is its frames over its sample rate."""
    assert wav_seconds(audio) == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("audio", "reason"),
    [
        (b"", "not a WAV"),
        (b"OggS" + b"\x00" * 64, "not a WAV"),
        (_wav(seconds=1)[:40], "not a WAV"),
        (_crafted_wav(format_tag=3, rate=16_000, bits=32), "not a WAV"),
        (_wav(seconds=1, sample_width=1), "8-bit samples; it must have 16-bit"),
        (_wav(seconds=1, sample_width=3), "24-bit samples; it must have 16-bit"),
        (_wav(seconds=1, channels=2), "2 channels; it must be mono"),
        (_crafted_wav(format_tag=1, rate=0, bits=16), "no sample rate"),
        (_wav(seconds=1)[:-2], "shorter than its header"),
    ],
    ids=["empty", "ogg", "header-cut", "float", "8-bit", "24-bit", "stereo", "no-rate", "truncated"],
)
def test_wav_seconds_refuses_anything_but_complete_16_bit_pcm_mono(audio: bytes, reason: str) -> None:
    """Only a whole 16-bit PCM mono WAV is accepted; the refusal says why."""
    with pytest.raises(WavError, match=reason):
        wav_seconds(audio)
