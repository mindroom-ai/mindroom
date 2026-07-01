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

_MEDIA_COMMAND_TIMEOUT_SECONDS = 30
_VOICE_WAVEFORM_SAMPLE_COUNT = 30
_VOICE_WAVEFORM_VALUE = 512
_VOICE_MIMETYPE = "audio/ogg"
_OPUS_RESPONSE_FORMAT = "opus"


@dataclass(frozen=True, slots=True)
class PreparedVoiceAudio:
    """Prepared Opus/Ogg payload for a Matrix voice message."""

    audio_bytes: bytes
    mimetype: str
    duration_ms: int | None
    waveform: list[int] | None


async def _kill_media_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    await proc.wait()


async def _run_media_command(*args: str) -> tuple[int, bytes] | None:
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=PIPE, stderr=PIPE)
    except OSError:
        return None
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=_MEDIA_COMMAND_TIMEOUT_SECONDS)
    except TimeoutError:
        await _kill_media_process(proc)
        return None
    except asyncio.CancelledError:
        await _kill_media_process(proc)
        raise
    return await proc.wait(), stdout


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
    return duration_ms if duration_ms > 0 else None


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


async def _probe_audio(ffprobe: str, audio_path: Path) -> dict[str, Any] | None:
    result = await _run_media_command(
        ffprobe,
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
    returncode, stdout = result
    if returncode != 0:
        return None
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


async def _transcode_to_opus_ogg(ffmpeg: str, input_path: Path) -> bytes | None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as output_handle:
        output_path = Path(output_handle.name)
    try:
        result = await _run_media_command(
            ffmpeg,
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
        if result is None or result[0] != 0:
            return None
        output_bytes = await asyncio.to_thread(output_path.read_bytes)
        return output_bytes or None
    finally:
        output_path.unlink(missing_ok=True)


def _write_audio_tempfile(audio_bytes: bytes, *, suffix: str) -> Path:
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as audio_handle:
        audio_handle.write(audio_bytes)
        return Path(audio_handle.name)


async def prepare_voice_audio_bytes(audio_bytes: bytes, *, response_format: str) -> PreparedVoiceAudio | None:
    """Prepare generated speech bytes as an Opus/Ogg Matrix voice payload.

    Opus input passes through unchanged and other formats are transcoded with ffmpeg.
    Without ffmpeg/ffprobe on PATH, opus input is returned without duration or waveform metadata and other formats return None.
    Returns None when the audio cannot be probed or transcoded.
    """
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        if response_format == _OPUS_RESPONSE_FORMAT:
            return PreparedVoiceAudio(
                audio_bytes=audio_bytes,
                mimetype=_VOICE_MIMETYPE,
                duration_ms=None,
                waveform=None,
            )
        return None

    input_path = await asyncio.to_thread(_write_audio_tempfile, audio_bytes, suffix=f".{response_format}")
    try:
        probe_payload = await _probe_audio(ffprobe, input_path)
        if probe_payload is None:
            return None
        duration_ms = _parse_duration_ms(probe_payload)
        waveform = _voice_waveform() if duration_ms is not None else None
        if _is_opus_ogg(probe_payload):
            return PreparedVoiceAudio(
                audio_bytes=audio_bytes,
                mimetype=_VOICE_MIMETYPE,
                duration_ms=duration_ms,
                waveform=waveform,
            )
        transcoded_bytes = await _transcode_to_opus_ogg(ffmpeg, input_path)
        if transcoded_bytes is None:
            return None
        return PreparedVoiceAudio(
            audio_bytes=transcoded_bytes,
            mimetype=_VOICE_MIMETYPE,
            duration_ms=duration_ms,
            waveform=waveform,
        )
    finally:
        input_path.unlink(missing_ok=True)
