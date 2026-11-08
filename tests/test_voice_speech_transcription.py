"""Speech-to-text through the voice settings, shared by voice messages and call transcripts."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.config.main import Config
from mindroom.config.voice import VoiceConfig, VoiceSTTConfig
from mindroom.llm.usage import TokenCounts
from mindroom.voice import handler as voice_handler
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_FORM_FIELD = re.compile(rb'Content-Disposition: form-data; name="([^"]+)"\r\n\r\n([^\r]*)\r\n')


def _config(**stt: object) -> Config:
    return Config(voice=VoiceConfig(stt=VoiceSTTConfig.model_validate({"api_key": "test-key", **stt})))


def _patch_stt(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Keep the HTTP client real and replace only the speech-to-text service."""
    requests: list[httpx.Request] = []
    client_type = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    monkeypatch.setattr(
        voice_handler.httpx,
        "AsyncClient",
        lambda *, timeout: client_type(transport=httpx.MockTransport(handle), timeout=timeout),
    )
    return requests


def _form_fields(request: httpx.Request) -> dict[str, str]:
    return {name.decode(): value.decode() for name, value in _FORM_FIELD.findall(request.read())}


@pytest.mark.asyncio
async def test_transcribe_speech_returns_the_text_and_its_usage_without_recording_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The caller gets the stripped text and the provider's token usage, and decides where the usage counts."""
    paths = test_runtime_paths(tmp_path)
    reply = {
        "text": " we ship on Friday ",
        "usage": {
            "type": "tokens",
            "input_tokens": 12,
            "output_tokens": 3,
            "total_tokens": 15,
            "input_token_details": {"audio_tokens": 9},
        },
    }
    requests = _patch_stt(monkeypatch, lambda _request: httpx.Response(200, json=reply))

    transcript = await voice_handler.transcribe_speech(b"RIFF-audio", _config(model="gpt-transcribe"), paths)

    assert transcript is not None
    assert transcript.text == "we ship on Friday"
    assert transcript.usage is not None
    assert transcript.usage.model == "gpt-transcribe"
    assert transcript.usage.provider == "openai"
    expected_tokens = TokenCounts(input_tokens=12, output_tokens=3, audio_input_tokens=9).with_totals()
    assert transcript.usage.metrics.tokens == expected_tokens
    assert not (paths.storage_root / "system").exists()
    [request] = requests
    assert b'name="file"; filename="audio.wav"\r\nContent-Type: audio/wav' in request.read()
    assert _form_fields(request) == {"model": "gpt-transcribe"}


@pytest.mark.asyncio
async def test_transcribe_speech_has_no_usage_when_the_provider_reports_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A provider that reports no token usage still gives the text."""
    _patch_stt(monkeypatch, lambda _request: httpx.Response(200, json={"text": "hello"}))

    transcript = await voice_handler.transcribe_speech(b"audio", _config(), test_runtime_paths(tmp_path))

    assert transcript == voice_handler.SpeechTranscript(text="hello", usage=None)


@pytest.mark.asyncio
async def test_transcribe_speech_sends_the_language_hint_over_the_configured_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A language hint is sent as the request's language, replacing one from the speech service's options."""
    requests = _patch_stt(monkeypatch, lambda _request: httpx.Response(200, json={"text": "hallo"}))
    config = _config(extra_kwargs={"language": "en", "temperature": 0})
    paths = test_runtime_paths(tmp_path)

    await voice_handler.transcribe_speech(b"audio", config, paths, language="nl")
    await voice_handler.transcribe_speech(b"audio", config, paths)

    hinted, unhinted = (_form_fields(request) for request in requests)
    assert hinted == {"model": "gpt-transcribe", "language": "nl", "temperature": "0"}
    assert unhinted == {"model": "gpt-transcribe", "language": "en", "temperature": "0"}


@pytest.mark.asyncio
async def test_transcribe_speech_sends_the_declared_audio_type(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Audio of another type is uploaded under that type."""
    requests = _patch_stt(monkeypatch, lambda _request: httpx.Response(200, json={"text": "hi"}))

    await voice_handler.transcribe_speech(b"audio", _config(), test_runtime_paths(tmp_path), mime_type="audio/ogg")

    assert b'name="file"; filename="audio.ogg"\r\nContent-Type: audio/ogg' in requests[0].read()


def _refused(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(503, text="unavailable")


def _unreachable(request: httpx.Request) -> httpx.Response:
    msg = "speech-to-text is down"
    raise httpx.ConnectError(msg, request=request)


@pytest.mark.parametrize("respond", [_refused, _unreachable], ids=["refused", "unreachable"])
@pytest.mark.asyncio
async def test_transcribe_speech_returns_none_when_the_service_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    respond: Callable[[httpx.Request], httpx.Response],
) -> None:
    """A failed request gives no transcript, so the caller can show the stretch as not transcribed."""
    requests = _patch_stt(monkeypatch, respond)

    assert await voice_handler.transcribe_speech(b"audio", _config(), test_runtime_paths(tmp_path)) is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_transcribe_speech_returns_none_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cloud speech-to-text without a resolvable key sends nothing."""
    requests = _patch_stt(monkeypatch, lambda _request: httpx.Response(200, json={"text": "unreachable"}))
    monkeypatch.setattr(voice_handler, "get_api_key_for_service", lambda *_args: None)
    config = Config(voice=VoiceConfig(stt=VoiceSTTConfig(credentials_service="openai")))

    assert await voice_handler.transcribe_speech(b"audio", config, test_runtime_paths(tmp_path)) is None
    assert requests == []
