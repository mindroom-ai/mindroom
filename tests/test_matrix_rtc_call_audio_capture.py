"""Opt-in call audio capture writes analyzable PCM off the event loop."""

from __future__ import annotations

import json
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import pytest
from livekit import rtc
from livekit.agents.voice.room_io._output import _ParticipantAudioOutput
from structlog.testing import capture_logs

from mindroom.constants import RuntimePaths
from mindroom.matrix_rtc.call_audio_capture import CallAudioCapture, call_audio_capture_dir, tap_published_audio

if TYPE_CHECKING:
    from livekit.agents import AgentSession


def _frame(sample: int, samples: int = 1200) -> rtc.AudioFrame:
    return rtc.AudioFrame(sample.to_bytes(2, "little", signed=True) * samples, 24000, 1, samples)


def _read(directory: Path, stream: str) -> tuple[bytes, list[dict[str, object]]]:
    with wave.open(str(directory / f"{stream}.wav"), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (24000, 1, 2)
        pcm = wav.readframes(wav.getnframes())
    lines = (directory / f"{stream}.jsonl").read_text(encoding="utf-8").splitlines()
    return pcm, [json.loads(line) for line in lines]


def _runtime_paths(**env: str) -> RuntimePaths:
    fake = Path("/var/empty/mindroom-test")
    return RuntimePaths(
        config_path=fake / "config.yaml",
        config_dir=fake,
        env_path=fake / ".env",
        storage_root=fake / "data",
        process_env=env,
    )


def test_capture_is_enabled_only_by_its_env_var(tmp_path: Path) -> None:
    """Without the variable there is nothing to create or write."""
    assert call_audio_capture_dir(_runtime_paths()) is None
    assert call_audio_capture_dir(_runtime_paths(MINDROOM_CALL_AUDIO_CAPTURE_DIR=str(tmp_path))) == tmp_path


@pytest.mark.asyncio
async def test_capture_writes_each_stream_as_wav_and_frame_log(tmp_path: Path) -> None:
    """Each stream gets 24 kHz mono PCM16 and one timed JSON line per frame."""
    with capture_logs() as logs:
        capture = CallAudioCapture(tmp_path)
        capture.provider_out(_frame(16384).data.tobytes())
        capture.mic_in(_frame(0), zero_filled=True)
        capture.mic_in(_frame(-16384, 600), zero_filled=False)
        capture.published(_frame(8192), queued_ms=150.04)
        await capture.aclose()
        capture.mic_in(_frame(1), zero_filled=False)

    assert capture.directory.parent == tmp_path
    assert [entry["event"] for entry in logs] == ["call_audio_capture_started", "call_audio_capture_closed"]
    assert logs[0]["path"] == str(capture.directory)

    provider_pcm, provider_lines = _read(capture.directory, "provider_out")
    assert provider_pcm == _frame(16384).data.tobytes()
    [line] = provider_lines
    assert set(line) == {"t_wall_ns", "t_mono_ns", "samples", "rms"}
    assert (line["samples"], line["rms"]) == (1200, 0.5)

    mic_pcm, mic_lines = _read(capture.directory, "mic_in")
    assert mic_pcm == _frame(0).data.tobytes() + _frame(-16384, 600).data.tobytes()
    assert [(line["samples"], line["rms"], line["zero_filled"]) for line in mic_lines] == [
        (1200, 0.0, True),
        (600, 0.5, False),
    ]
    assert mic_lines[0]["t_mono_ns"] <= mic_lines[1]["t_mono_ns"]

    _published_pcm, published_lines = _read(capture.directory, "published")
    assert [(line["samples"], line["queued_ms"]) for line in published_lines] == [(1200, 150.0)]


@pytest.mark.asyncio
async def test_published_tap_records_frames_accepted_by_the_native_audio_source(tmp_path: Path) -> None:
    """Frames reach the native source unchanged and are logged with its queued duration."""
    output = _ParticipantAudioOutput(
        MagicMock(),
        sample_rate=24000,
        num_channels=1,
        track_publish_options=rtc.TrackPublishOptions(),
    )
    session = cast("AgentSession", SimpleNamespace(output=SimpleNamespace(audio=output)))
    capture = CallAudioCapture(tmp_path)
    try:
        tap_published_audio(session, capture)
        for _ in range(2):
            await output._audio_source.capture_frame(_frame(4096))
        assert output._audio_source.queued_duration > 0.05
    finally:
        await capture.aclose()
        await output._audio_source.aclose()

    pcm, lines = _read(capture.directory, "published")
    assert pcm == _frame(4096).data.tobytes() * 2
    assert [line["samples"] for line in lines] == [1200, 1200]
    assert 40 < lines[0]["queued_ms"] < lines[1]["queued_ms"] <= 100


@pytest.mark.asyncio
async def test_published_tap_skips_sessions_without_room_audio_output(tmp_path: Path) -> None:
    """A headless session has no native source; capture continues for the other streams."""
    session = cast("AgentSession", SimpleNamespace(output=SimpleNamespace(audio=None)))
    capture = CallAudioCapture(tmp_path)
    with capture_logs() as logs:
        tap_published_audio(session, capture)
    await capture.aclose()

    assert [entry["event"] for entry in logs] == ["call_audio_capture_published_unavailable"]
