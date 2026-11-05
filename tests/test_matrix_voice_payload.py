"""Tests for Matrix voice-message payload preparation."""

from __future__ import annotations

import shutil
import subprocess
from array import array
from typing import TYPE_CHECKING

import pytest

from mindroom.matrix.voice_message import (
    _is_opus_ogg,
    _parse_duration_ms,
    _voice_waveform,
    _waveform_levels,
    can_prepare_voice_audio,
    prepare_voice_audio_bytes,
)

if TYPE_CHECKING:
    from pathlib import Path

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


def _generate_audio_bytes(*ffmpeg_output_args: str, source: str = "sine=frequency=440:duration=1") -> bytes:
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", source, *ffmpeg_output_args, "-"],
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
@pytest.mark.parametrize(
    ("response_format", "ffmpeg_output_args"),
    [("opus", ("-c:a", "libopus", "-f", "ogg")), ("wav", ("-f", "wav"))],
)
async def test_prepare_voice_audio_bytes_waveform_follows_loudness(
    response_format: str,
    ffmpeg_output_args: tuple[str, ...],
) -> None:
    """The waveform should be loud during a tone and near zero during the silence after it."""
    audio_bytes = _generate_audio_bytes(
        *ffmpeg_output_args,
        source="sine=frequency=440:duration=1,apad=pad_dur=1",
    )

    prepared = await prepare_voice_audio_bytes(audio_bytes, response_format=response_format)

    assert prepared is not None
    assert prepared.waveform is not None
    assert len(prepared.waveform) == 100
    assert max(prepared.waveform) == 1024
    tone, silence = prepared.waveform[:45], prepared.waveform[55:]
    assert min(tone) > 900
    assert max(silence) < 20


@requires_ffmpeg
@pytest.mark.asyncio
async def test_voice_waveform_is_none_when_ffmpeg_cannot_decode(tmp_path: Path) -> None:
    """Audio that ffmpeg cannot decode should get no waveform instead of a made-up one."""
    audio_path = tmp_path / "broken.ogg"
    audio_path.write_bytes(b"not audio at all")

    assert await _voice_waveform(shutil.which("ffmpeg") or "ffmpeg", audio_path) is None


def test_waveform_levels_of_silence_are_zero() -> None:
    """Silent audio should produce a zero waveform without dividing by zero."""
    assert _waveform_levels(array("h", [0] * 8000)) == [0] * 100


def test_waveform_levels_need_one_sample_per_bucket() -> None:
    """Audio shorter than one sample per bucket should get no waveform."""
    assert _waveform_levels(array("h", [1000] * 99)) is None


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


@pytest.mark.parametrize(("response_format", "expected"), [("opus", True), ("wav", False), ("mp3", False)])
def test_only_opus_can_be_prepared_without_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
    response_format: str,
    *,
    expected: bool,
) -> None:
    """Without ffmpeg and ffprobe, only opus speech can become a voice note."""
    monkeypatch.setattr("mindroom.matrix.voice_message.shutil.which", lambda _name: None)

    assert can_prepare_voice_audio(response_format) is expected


@pytest.mark.parametrize("missing", ["ffmpeg", "ffprobe"])
def test_non_opus_needs_both_media_tools(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    """Missing either ffmpeg or ffprobe rules out non-opus speech."""
    monkeypatch.setattr(
        "mindroom.matrix.voice_message.shutil.which",
        lambda name: None if name == missing else f"/usr/bin/{name}",
    )

    assert not can_prepare_voice_audio("wav")


def test_any_format_can_be_prepared_with_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """With ffmpeg and ffprobe, non-opus speech can become a voice note."""
    monkeypatch.setattr("mindroom.matrix.voice_message.shutil.which", lambda name: f"/usr/bin/{name}")

    assert can_prepare_voice_audio("wav")
