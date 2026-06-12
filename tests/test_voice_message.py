"""Tests for Matrix voice-message payload preparation."""

from __future__ import annotations

import asyncio
import json
import shutil
import wave
from asyncio.subprocess import PIPE
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.matrix import voice_message
from mindroom.matrix.voice_message import VoiceMessagePayload, build_voice_message_payload, voice_event_extra_content

if TYPE_CHECKING:
    from collections.abc import Coroutine


class _FakeProcess:
    def __init__(self, *, returncode: int | None, stdout: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.killed = False
        self.waited = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        self.returncode = -9
        return self.returncode


class _CancellingFakeProcess(_FakeProcess):
    async def communicate(self) -> tuple[bytes, bytes]:
        raise asyncio.CancelledError


def _probe_payload(
    *,
    duration: str = "1.000000",
    codec_name: str = "pcm_s16le",
    format_name: str = "wav",
) -> bytes:
    return json.dumps(
        {
            "streams": [{"codec_name": codec_name, "codec_type": "audio"}],
            "format": {"duration": duration, "format_name": format_name},
        },
    ).encode()


@pytest.mark.asyncio
async def test_build_voice_message_payload_returns_none_on_ffmpeg_missing(tmp_path: Path) -> None:
    """Missing ffmpeg should make voice preparation fall back."""
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake")

    with patch("mindroom.matrix.voice_message._FFMPEG", None):
        assert await build_voice_message_payload(audio) is None


@pytest.mark.asyncio
async def test_build_voice_message_payload_returns_none_on_ffprobe_failure(tmp_path: Path) -> None:
    """A failed ffprobe command should make voice preparation fall back."""
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake")

    async def fake_create_subprocess_exec(*_args: str, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(returncode=1)

    with (
        patch("mindroom.matrix.voice_message._FFMPEG", "ffmpeg"),
        patch("mindroom.matrix.voice_message._FFPROBE", "ffprobe"),
        patch("mindroom.matrix.voice_message.asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
    ):
        assert await build_voice_message_payload(audio) is None


@pytest.mark.asyncio
async def test_build_voice_message_payload_returns_none_on_ffmpeg_timeout(tmp_path: Path) -> None:
    """Timed-out ffmpeg work should kill the child process and fall back."""
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake")
    probe = _FakeProcess(returncode=0, stdout=_probe_payload())
    ffmpeg = _FakeProcess(returncode=None)

    async def fake_create_subprocess_exec(*args: str, **_kwargs: object) -> _FakeProcess:
        return probe if args[0] == "ffprobe" else ffmpeg

    wait_calls = 0

    async def fake_wait_for(
        awaitable: Coroutine[object, object, tuple[bytes, bytes]],
        **_kwargs: object,
    ) -> tuple[bytes, bytes]:
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 2:
            awaitable.close()
            raise TimeoutError
        return await awaitable

    with (
        patch("mindroom.matrix.voice_message._FFMPEG", "ffmpeg"),
        patch("mindroom.matrix.voice_message._FFPROBE", "ffprobe"),
        patch("mindroom.matrix.voice_message.asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
        patch("mindroom.matrix.voice_message.asyncio.wait_for", side_effect=fake_wait_for),
    ):
        assert await build_voice_message_payload(audio) is None

    assert ffmpeg.killed is True
    assert ffmpeg.waited is True


@pytest.mark.asyncio
async def test_run_media_command_kills_process_on_cancellation() -> None:
    """Cancelled media commands should not leave the child process running."""
    proc = _CancellingFakeProcess(returncode=None)

    async def fake_create_subprocess_exec(*_args: str, **_kwargs: object) -> _FakeProcess:
        return proc

    with (
        patch("mindroom.matrix.voice_message.asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
        pytest.raises(asyncio.CancelledError),
    ):
        await voice_message._run_media_command("ffmpeg")

    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_build_voice_message_payload_returns_none_on_zero_size_output(tmp_path: Path) -> None:
    """A zero-byte ffmpeg output should be rejected."""
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake")
    probe = _FakeProcess(returncode=0, stdout=_probe_payload())
    ffmpeg = _FakeProcess(returncode=0)

    async def fake_create_subprocess_exec(*args: str, **_kwargs: object) -> _FakeProcess:
        return probe if args[0] == "ffprobe" else ffmpeg

    with (
        patch("mindroom.matrix.voice_message._FFMPEG", "ffmpeg"),
        patch("mindroom.matrix.voice_message._FFPROBE", "ffprobe"),
        patch("mindroom.matrix.voice_message.asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
    ):
        assert await build_voice_message_payload(audio) is None


@pytest.mark.asyncio
async def test_transcode_voice_payload_removes_tempfile_on_cancellation(tmp_path: Path) -> None:
    """Cancelled transcoding should remove the incomplete output file."""
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake")
    output = tmp_path / "prepared.ogg"

    class FakeNamedTemporaryFile:
        name = str(output)

        def __enter__(self) -> FakeNamedTemporaryFile:
            output.write_bytes(b"partial")
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    async def fake_run_media_command(*_args: str) -> tuple[int, bytes, bytes] | None:
        raise asyncio.CancelledError

    with (
        patch("mindroom.matrix.voice_message._FFMPEG", "ffmpeg"),
        patch("mindroom.matrix.voice_message.tempfile.NamedTemporaryFile", return_value=FakeNamedTemporaryFile()),
        patch("mindroom.matrix.voice_message._run_media_command", side_effect=fake_run_media_command),
        pytest.raises(asyncio.CancelledError),
    ):
        await voice_message._transcode_voice_payload(audio, duration_ms=1000, waveform=[512] * 30)

    assert not output.exists()


@pytest.mark.asyncio
async def test_build_voice_message_payload_returns_none_on_duration_over_soft_cap(tmp_path: Path) -> None:
    """Audio over five minutes should stay as a generic audio attachment."""
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fake")

    async def fake_create_subprocess_exec(*_args: str, **_kwargs: object) -> _FakeProcess:
        return _FakeProcess(returncode=0, stdout=_probe_payload(duration="360.000000"))

    with (
        patch("mindroom.matrix.voice_message._FFMPEG", "ffmpeg"),
        patch("mindroom.matrix.voice_message._FFPROBE", "ffprobe"),
        patch("mindroom.matrix.voice_message.asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
    ):
        assert await build_voice_message_payload(audio) is None


@pytest.mark.asyncio
async def test_build_voice_message_payload_skips_already_opus(tmp_path: Path) -> None:
    """Opus audio in an Ogg container should be sent without transcoding."""
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"fake ogg")
    ffprobe_calls: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*args: str, **_kwargs: object) -> _FakeProcess:
        ffprobe_calls.append(args)
        return _FakeProcess(returncode=0, stdout=_probe_payload(codec_name="opus", format_name="ogg"))

    with (
        patch("mindroom.matrix.voice_message._FFMPEG", "ffmpeg"),
        patch("mindroom.matrix.voice_message._FFPROBE", "ffprobe"),
        patch("mindroom.matrix.voice_message.asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec),
    ):
        payload = await build_voice_message_payload(audio)

    assert ffprobe_calls == [
        (
            "ffprobe",
            "-v",
            "error",
            "-of",
            "json",
            "-show_entries",
            "format=duration,format_name:stream=codec_name,codec_type",
            str(audio.resolve()),
        ),
    ]
    assert payload == VoiceMessagePayload(
        source_path=audio.resolve(),
        cleanup=False,
        duration_ms=1000,
        waveform=[512] * 30,
        mimetype="audio/ogg",
    )


def test_voice_event_extra_content_returns_all_msc_keys() -> None:
    """Voice event extras should include MSC1767, MSC3245, and stable voice keys."""
    payload = VoiceMessagePayload(
        source_path=Path("voice.ogg"),
        cleanup=False,
        duration_ms=1234,
        waveform=[512] * 30,
        mimetype="audio/ogg",
    )

    content = voice_event_extra_content(payload)

    assert content == {
        "org.matrix.msc1767.audio": {"duration": 1234, "waveform": [512] * 30},
        "org.matrix.msc3245.voice": {},
        "m.voice": {},
    }


def test_voice_event_extra_content_waveform_is_30_bounded_ints() -> None:
    """Voice event waveform metadata should be fixed-length and bounded."""
    payload = VoiceMessagePayload(
        source_path=Path("voice.ogg"),
        cleanup=False,
        duration_ms=1234,
        waveform=[512] * 30,
        mimetype="audio/ogg",
    )

    waveform = voice_event_extra_content(payload)["org.matrix.msc1767.audio"]["waveform"]

    assert len(waveform) == 30
    assert all(isinstance(sample, int) and 0 <= sample <= 1024 for sample in waveform)


@pytest.mark.asyncio
async def test_build_voice_message_payload_transcodes_wav(tmp_path: Path) -> None:
    """A real short WAV should transcode to an Opus/Ogg tempfile when ffmpeg is available."""
    if voice_message._FFMPEG is None or voice_message._FFPROBE is None:
        pytest.skip("ffmpeg and ffprobe are required for real voice payload integration")

    audio = tmp_path / "silent.wav"
    with wave.open(str(audio), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(b"\x00\x00" * 16000)

    payload = await build_voice_message_payload(audio)

    assert payload is not None
    try:
        assert payload.source_path.exists()
        assert payload.source_path.stat().st_size > 0
        assert payload.cleanup is True
        assert 0 < payload.duration_ms <= 5000
        assert payload.waveform == [512] * 30
        assert payload.mimetype == "audio/ogg"
    finally:
        if payload is not None and payload.cleanup:
            payload.source_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_build_voice_message_payload_skips_real_opus_ogg(tmp_path: Path) -> None:
    """A real Opus/Ogg file should use the original file without transcoding."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("ffmpeg and ffprobe are required for real opus/ogg skip integration")

    audio = tmp_path / "voice.ogg"
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=mono",
        "-t",
        "0.5",
        "-c:a",
        "libopus",
        str(audio),
        stdout=PIPE,
        stderr=PIPE,
    )
    _stdout, stderr = await proc.communicate()
    assert proc.returncode == 0, stderr.decode("utf-8", errors="replace")

    payload = await build_voice_message_payload(audio)

    assert payload is not None
    assert payload.source_path == audio.resolve()
    assert payload.cleanup is False
    assert 0 < payload.duration_ms <= 5000
    assert payload.waveform == [512] * 30
    assert payload.mimetype == "audio/ogg"
