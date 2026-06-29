"""Matrix voice-message payload preparation helpers."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from asyncio.subprocess import PIPE
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FFMPEG = shutil.which("ffmpeg")
_FFPROBE = shutil.which("ffprobe")
_VOICE_TIMEOUT_SECONDS = 30
_MAX_VOICE_DURATION_MS = 5 * 60 * 1000
_VOICE_WAVEFORM_SAMPLE_COUNT = 30
_VOICE_WAVEFORM_VALUE = 512
_VOICE_MIMETYPE = "audio/ogg"


@dataclass(frozen=True, slots=True)
class VoiceMessagePayload:
    """Prepared Opus/Ogg payload metadata for a Matrix voice message."""

    source_path: Path
    cleanup: bool
    duration_ms: int
    waveform: list[int]
    mimetype: str


async def _kill_media_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    await proc.wait()


def _unlink_tempfile(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return


async def _run_media_command(*args: str) -> tuple[int, bytes, bytes] | None:
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=PIPE, stderr=PIPE)
    except OSError:
        return None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_VOICE_TIMEOUT_SECONDS)
    except TimeoutError:
        await _kill_media_process(proc)
        return None
    except asyncio.CancelledError:
        await _kill_media_process(proc)
        raise
    if proc.returncode is None:
        return None
    return proc.returncode, stdout, stderr


def _parse_duration_ms(probe_payload: dict[str, Any]) -> int | None:
    format_payload = probe_payload.get("format")
    if not isinstance(format_payload, dict):
        return None
    duration_value = format_payload.get("duration")
    if not isinstance(duration_value, str):
        return None
    try:
        duration_ms = round(float(duration_value) * 1000)
    except ValueError:
        return None
    if duration_ms <= 0 or duration_ms > _MAX_VOICE_DURATION_MS:
        return None
    return duration_ms


def _is_opus_ogg(probe_payload: dict[str, Any]) -> bool:
    streams = probe_payload.get("streams")
    if not isinstance(streams, list):
        return False
    has_opus_audio = any(
        isinstance(stream, dict) and stream.get("codec_type") == "audio" and stream.get("codec_name") == "opus"
        for stream in streams
    )
    format_payload = probe_payload.get("format")
    format_name = format_payload.get("format_name") if isinstance(format_payload, dict) else None
    return has_opus_audio and isinstance(format_name, str) and "ogg" in format_name.split(",")


def _voice_waveform() -> list[int]:
    return [_VOICE_WAVEFORM_VALUE] * _VOICE_WAVEFORM_SAMPLE_COUNT


async def _probe_audio(audio_path: Path) -> dict[str, Any] | None:
    if _FFPROBE is None:
        return None
    result = await _run_media_command(
        _FFPROBE,
        "-v",
        "error",
        "-of",
        "json",
        "-show_entries",
        "format=duration,format_name:stream=codec_name,codec_type",
        str(audio_path),
    )
    if result is None:
        return None
    returncode, stdout, _stderr = result
    if returncode != 0:
        return None
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


async def _transcode_voice_payload(
    input_path: Path,
    *,
    duration_ms: int,
    waveform: list[int],
) -> VoiceMessagePayload | None:
    if _FFMPEG is None:
        return None
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tempfile_handle:
        output_path = Path(tempfile_handle.name)

    keep_output = False
    try:
        result = await _run_media_command(
            _FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(input_path),
            "-vn",
            "-ac",
            "1",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-application",
            "voip",
            "-map_metadata",
            "-1",
            str(output_path),
        )
        valid_output = result is not None and result[0] == 0 and output_path.exists() and output_path.stat().st_size > 0
        if not valid_output:
            return None
        keep_output = True
        return VoiceMessagePayload(
            source_path=output_path,
            cleanup=True,
            duration_ms=duration_ms,
            waveform=waveform,
            mimetype=_VOICE_MIMETYPE,
        )
    finally:
        if not keep_output:
            _unlink_tempfile(output_path)


async def build_voice_message_payload(audio_path: Path) -> VoiceMessagePayload | None:
    """Return prepared voice payload, or None so callers can fall back to plain m.audio."""
    if _FFMPEG is None or _FFPROBE is None:
        return None

    input_path = audio_path.expanduser().resolve()
    probe_payload = await _probe_audio(input_path)
    if probe_payload is None:
        return None
    duration_ms = _parse_duration_ms(probe_payload)
    if duration_ms is None:
        return None
    waveform = _voice_waveform()
    if _is_opus_ogg(probe_payload):
        return VoiceMessagePayload(
            source_path=input_path,
            cleanup=False,
            duration_ms=duration_ms,
            waveform=waveform,
            mimetype=_VOICE_MIMETYPE,
        )
    return await _transcode_voice_payload(input_path, duration_ms=duration_ms, waveform=waveform)
