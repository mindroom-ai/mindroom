"""Speech-to-text through the voice settings, shared by voice messages and call transcripts."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx
import pytest
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.config.voice import VoiceConfig, VoiceSTTConfig
from mindroom.llm.usage import TokenCounts
from mindroom.model_defaults import LOCAL_OPENAI_API_KEY_DEFAULT, OPENAI_BASE_URL_DEFAULT
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


def test_resolve_stt_connection_handles_providers_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """STT connection resolution handles OpenAI keys, credential services, and keyless compatible hosts."""
    paths = test_runtime_paths(tmp_path)
    monkeypatch.setattr(
        voice_handler,
        "get_api_key_for_service",
        lambda service, _paths: "svc-key" if service == "custom" else None,
    )

    assert voice_handler.resolve_stt_connection(_config(api_key="direct-key"), paths) == (
        f"{OPENAI_BASE_URL_DEFAULT}/audio/transcriptions",
        "direct-key",
    )
    assert (
        voice_handler.resolve_stt_connection(
            Config(voice=VoiceConfig(stt=VoiceSTTConfig(credentials_service="openai"))),
            paths,
        )
        is None
    )
    assert voice_handler.resolve_stt_connection(
        Config(voice=VoiceConfig(stt=VoiceSTTConfig(provider="openai_compatible", host="http://localhost:8080"))),
        paths,
    ) == ("http://localhost:8080/v1/audio/transcriptions", LOCAL_OPENAI_API_KEY_DEFAULT)
    assert voice_handler.resolve_stt_connection(
        Config(
            voice=VoiceConfig(
                stt=VoiceSTTConfig(
                    provider="openai_compatible",
                    host="https://stt.test/v1",
                    credentials_service="custom",
                ),
            ),
        ),
        paths,
    ) == ("https://stt.test/v1/audio/transcriptions", "svc-key")


@pytest.mark.asyncio
async def test_transcribe_speech_preserves_operator_prompt_and_skips_diarize(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Operator extra_kwargs.prompt is preserved (up to 400 chars) with dictation tail <= 800; diarize omits prompt."""
    requests = _patch_stt(monkeypatch, lambda _request: httpx.Response(200, json={"text": "ok"}))
    paths = test_runtime_paths(tmp_path)

    await voice_handler.transcribe_speech(b"audio", _config(), paths, prompt="  MindRoom dictation  ")
    await voice_handler.transcribe_speech(b"audio", _config(), paths, prompt="   ")
    vocab = "Vocabulary: MindRoom, Tuwunel"
    long_dictation = "recent " * 140 + "TAIL"
    await voice_handler.transcribe_speech(
        b"audio",
        _config(extra_kwargs={"prompt": vocab}),
        paths,
        prompt=long_dictation,
    )
    huge_base = "OP_START " + "b" * 500
    await voice_handler.transcribe_speech(
        b"audio",
        _config(extra_kwargs={"prompt": huge_base}),
        paths,
        prompt=long_dictation,
    )
    await voice_handler.transcribe_speech(
        b"audio",
        _config(model="gpt-4o-transcribe-diarize", extra_kwargs={"prompt": vocab}),
        paths,
        prompt="should be omitted",
    )

    direct, blank, preserved, capped, diarize = (_form_fields(request) for request in requests)
    assert direct["prompt"] == "MindRoom dictation"
    assert "prompt" not in blank
    assert preserved["prompt"].startswith(f"{vocab} ")
    assert preserved["prompt"].endswith("TAIL")
    assert len(preserved["prompt"]) <= 800
    assert capped["prompt"].startswith(f"{huge_base[:400]} ")
    assert capped["prompt"].endswith("TAIL")
    assert len(capped["prompt"]) <= 800
    assert "prompt" not in diarize


@pytest.mark.asyncio
async def test_transcribe_speech_reuses_http_client_without_closing_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A supplied pooled client is reused with the requested timeout and never closed by transcribe_speech."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"text": "pooled"})

    def unexpected_client(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        msg = "transcribe_speech must reuse the provided http_client"
        raise AssertionError(msg)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as pooled:
        monkeypatch.setattr(voice_handler.httpx, "AsyncClient", unexpected_client)
        transcript = await voice_handler.transcribe_speech(
            b"audio",
            _config(),
            test_runtime_paths(tmp_path),
            timeout_seconds=25.0,
            http_client=pooled,
        )
        assert not pooled.is_closed

    assert transcript == voice_handler.SpeechTranscript(text="pooled", usage=None)
    assert len(requests) == 1
    assert requests[0].extensions["timeout"] == {"connect": 5.0, "read": 25.0, "write": 10.0, "pool": 5.0}


@pytest.mark.asyncio
async def test_transcribe_speech_omits_response_text_from_error_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Non-200 STT errors log only the status code so provider error bodies never leak dictated text."""
    private_phrase = "super-private-dictated-sentence"
    _patch_stt(monkeypatch, lambda _request: httpx.Response(422, text=f"Invalid transcript: {private_phrase}"))

    with capture_logs() as logs:
        transcript = await voice_handler.transcribe_speech(b"audio", _config(), test_runtime_paths(tmp_path))

    assert transcript is None
    [error_log] = [entry for entry in logs if entry.get("event") == "stt_api_error"]
    assert error_log["status_code"] == 422
    assert "error" not in error_log
    assert private_phrase not in str(logs)
