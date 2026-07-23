"""Tests for Matrix voice-message payload preparation."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from mindroom.matrix.voice_message import (
    _is_opus_ogg,
    _parse_duration_ms,
    prepare_voice_audio_bytes,
)

_FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
requires_ffmpeg = pytest.mark.skipif(not _FFMPEG_AVAILABLE, reason="ffmpeg and ffprobe are required")


@pytest.mark.parametrize(
    ("probe_payload", "expected"),
    [
        ({}, None),
        ({"format": "not-a-dict"}, None),
        ({"format": {}}, None),
        ({"format": {"duration": 1.25}}, None),
        ({"format": {"duration": "not-a-number"}}, None),
        ({"format": {"duration": "0"}}, None),
        ({"format": {"duration": "-1.0"}}, None),
        ({"format": {"duration": "1.25"}}, 1250),
        ({"format": {"duration": "360.0"}}, 360_000),
    ],
)
def test_parse_duration_ms(probe_payload: dict[str, object], expected: int | None) -> None:
    """Durations should parse from ffprobe format payloads, including long audio."""
    assert _parse_duration_ms(probe_payload) == expected


@pytest.mark.parametrize(
    ("probe_payload", "expected"),
    [
        ({}, False),
        ({"streams": "not-a-list"}, False),
        ({"streams": [{"codec_type": "audio", "codec_name": "opus"}]}, False),
        (
            {"streams": [{"codec_type": "audio", "codec_name": "opus"}], "format": {"format_name": "ogg"}},
            True,
        ),
        (
            {"streams": [{"codec_type": "audio", "codec_name": "vorbis"}], "format": {"format_name": "ogg"}},
            False,
        ),
        (
            {"streams": [{"codec_type": "audio", "codec_name": "opus"}], "format": {"format_name": "matroska,webm"}},
            False,
        ),
    ],
)
def test_is_opus_ogg(probe_payload: dict[str, object], expected: bool) -> None:
    """Opus/Ogg detection should require an opus audio stream in an ogg container."""
    assert _is_opus_ogg(probe_payload) == expected


def _generate_audio_bytes(*ffmpeg_output_args: str) -> bytes:
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", *ffmpeg_output_args, "-"],
        capture_output=True,
        check=True,
    )
    return result.stdout


@requires_ffmpeg
@pytest.mark.asyncio
async def test_prepare_voice_audio_bytes_passes_opus_through() -> None:
    """Opus/Ogg input should be returned unchanged with probed duration metadata."""
    opus_bytes = _generate_audio_bytes("-c:a", "libopus", "-f", "ogg")

    prepared = await prepare_voice_audio_bytes(opus_bytes, response_format="opus")

    assert prepared is not None
    assert prepared.audio_bytes == opus_bytes
    assert prepared.mimetype == "audio/ogg"
    assert prepared.duration_ms is not None
    assert 900 <= prepared.duration_ms <= 1100
    assert prepared.waveform is not None


@requires_ffmpeg
@pytest.mark.asyncio
async def test_prepare_voice_audio_bytes_transcodes_wav_to_opus_ogg() -> None:
    """Non-opus input should be transcoded into an Opus/Ogg voice payload."""
    wav_bytes = _generate_audio_bytes("-f", "wav")

    prepared = await prepare_voice_audio_bytes(wav_bytes, response_format="wav")

    assert prepared is not None
    assert prepared.audio_bytes != wav_bytes
    assert prepared.audio_bytes.startswith(b"OggS")
    assert prepared.mimetype == "audio/ogg"
    assert prepared.duration_ms is not None
    assert 900 <= prepared.duration_ms <= 1100
    assert prepared.waveform is not None


@requires_ffmpeg
@pytest.mark.asyncio
async def test_prepare_voice_audio_bytes_rejects_unreadable_audio() -> None:
    """Bytes that ffprobe cannot identify should not produce a payload."""
    assert await prepare_voice_audio_bytes(b"not audio at all", response_format="wav") is None


@pytest.mark.asyncio
async def test_prepare_voice_audio_bytes_opus_falls_back_without_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opus input should still be deliverable without ffmpeg, minus metadata."""
    monkeypatch.setattr("mindroom.matrix.voice_message.shutil.which", lambda _name: None)

    prepared = await prepare_voice_audio_bytes(b"opus-bytes", response_format="opus")

    assert prepared is not None
    assert prepared.audio_bytes == b"opus-bytes"
    assert prepared.mimetype == "audio/ogg"
    assert prepared.duration_ms is None
    assert prepared.waveform is None


@pytest.mark.asyncio
async def test_prepare_voice_audio_bytes_non_opus_requires_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-opus input cannot be prepared without ffmpeg and ffprobe."""
    monkeypatch.setattr("mindroom.matrix.voice_message.shutil.which", lambda _name: None)

    assert await prepare_voice_audio_bytes(b"wav-bytes", response_format="wav") is None
