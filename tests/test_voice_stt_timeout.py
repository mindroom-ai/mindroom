"""STT request budgets, failure handling, and cancellation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom import voice_handler
from mindroom.config.main import Config
from mindroom.config.voice import VoiceConfig, VoiceSTTConfig
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path


@pytest.fixture
def stt_config() -> Config:
    """Use a local-compatible endpoint without external credentials."""
    return Config(
        voice=VoiceConfig(
            enabled=True,
            stt=VoiceSTTConfig(provider="openai_compatible", host="http://stt.example.test"),
        ),
    )


def _patch_stt_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
) -> list[httpx.Request]:
    """Keep the HTTP client real and replace only the external STT transport."""
    requests: list[httpx.Request] = []
    async_client_type = httpx.AsyncClient

    async def record_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return await handler(request)

    def client_factory(*, timeout: httpx.Timeout) -> httpx.AsyncClient:
        return async_client_type(transport=httpx.MockTransport(record_request), timeout=timeout)

    monkeypatch.setattr(voice_handler.httpx, "AsyncClient", client_factory)
    return requests


@pytest.mark.asyncio
async def test_stt_uses_short_connection_timeout_and_longer_read_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stt_config: Config,
) -> None:
    """Slow transcription gets a read allowance without extending connection waits."""

    async def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": " transcript "})

    requests = _patch_stt_transport(monkeypatch, handle_request)
    result = await voice_handler._transcribe_audio(b"audio", stt_config, test_runtime_paths(tmp_path))

    assert result == "transcript"
    assert len(requests) == 1
    assert requests[0].extensions["timeout"] == {"connect": 5.0, "read": 60.0, "write": 10.0, "pool": 5.0}


@pytest.mark.parametrize(
    "error_type",
    [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, httpx.ConnectError, None],
)
@pytest.mark.asyncio
async def test_stt_failures_return_fallback_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stt_config: Config,
    error_type: type[httpx.RequestError] | None,
) -> None:
    """Failed requests never submit the same audio to the server twice."""

    async def handle_request(request: httpx.Request) -> httpx.Response:
        if error_type is not None:
            msg = "STT unavailable"
            raise error_type(msg, request=request)
        return httpx.Response(503, text="STT unavailable")

    requests = _patch_stt_transport(monkeypatch, handle_request)
    result = await voice_handler._transcribe_audio(b"audio", stt_config, test_runtime_paths(tmp_path))

    assert result is None
    assert len(requests) == 1


@pytest.mark.parametrize("slow_body", [False, True], ids=["response-headers", "response-body"])
@pytest.mark.asyncio
async def test_stt_total_deadline_bounds_response_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stt_config: Config,
    slow_body: bool,
) -> None:
    """A stalled header or response body cannot outlive the complete STT budget."""
    monkeypatch.setattr(voice_handler, "_STT_TOTAL_TIMEOUT_SECONDS", 0.01)
    body_closed = asyncio.Event()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'{"text": "'
            await asyncio.sleep(0.1)
            yield b'late transcript"}'

        async def aclose(self) -> None:
            body_closed.set()

    async def handle_request(_request: httpx.Request) -> httpx.Response:
        if slow_body:
            return httpx.Response(200, stream=SlowBody())
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"text": "late transcript"})

    requests = _patch_stt_transport(monkeypatch, handle_request)
    result = await voice_handler._transcribe_audio(b"audio", stt_config, test_runtime_paths(tmp_path))

    assert result is None
    assert len(requests) == 1
    if slow_body:
        assert body_closed.is_set()


@pytest.mark.asyncio
async def test_stt_cancellation_propagates_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stt_config: Config,
) -> None:
    """External cancellation must not become a fallback or a second transcription."""
    started = asyncio.Event()

    async def handle_request(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        return httpx.Response(200, json={"text": "unreachable"})

    requests = _patch_stt_transport(monkeypatch, handle_request)
    task = asyncio.create_task(voice_handler._transcribe_audio(b"audio", stt_config, test_runtime_paths(tmp_path)))
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(requests) == 1
