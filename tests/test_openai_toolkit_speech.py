"""Tests for the ``openai`` toolkit's ``generate_speech()`` on Realtime and speech-endpoint models."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from agno.tools import openai as agno_openai

from mindroom.tools import agno_compat_openai
from mindroom.tools.agno_compat_openai import MindRoomOpenAITools
from mindroom.voice.openai_realtime_speech import pcm_to_wav

if TYPE_CHECKING:
    from agno.tools.function import ToolResult


async def _generate_speech(toolkit: MindRoomOpenAITools, text: str) -> ToolResult:
    """Call ``generate_speech`` the way MindRoom offers it to the agent loop."""
    function = toolkit.get_async_functions()["generate_speech"]
    assert function.entrypoint is not None
    if inspect.iscoroutinefunction(function.entrypoint):
        return await function.entrypoint(text_input=text)
    return function.entrypoint(agent=None, text_input=text)


@pytest.fixture
def synthesize(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the Realtime synthesizer with one that returns two PCM bytes."""
    mock = AsyncMock(return_value=b"\x01\x00")
    monkeypatch.setattr(agno_compat_openai, "synthesize_realtime_speech", mock)
    return mock


@pytest.mark.asyncio
async def test_realtime_speech_returns_wav_audio(synthesize: AsyncMock) -> None:
    """A Realtime model's speech comes back as one WAV audio attachment."""
    toolkit = MindRoomOpenAITools(api_key="sk-test", text_to_speech_model="gpt-realtime-2.1-mini")

    result = await _generate_speech(toolkit, "Read this.")

    synthesize.assert_awaited_once_with("Read this.", model="gpt-realtime-2.1-mini", voice="alloy", api_key="sk-test")
    assert result.audios is not None
    assert len(result.audios) == 1
    assert result.audios[0].mime_type == "audio/wav"
    assert result.audios[0].content == pcm_to_wav(b"\x01\x00")


@pytest.mark.asyncio
async def test_realtime_speech_pcm_returns_raw_pcm(synthesize: AsyncMock) -> None:
    """The ``pcm`` format returns the Realtime samples unwrapped."""
    toolkit = MindRoomOpenAITools(
        api_key="sk-test",
        text_to_speech_model="gpt-realtime-2.1-mini",
        text_to_speech_format="pcm",
    )

    result = await _generate_speech(toolkit, "Read this.")

    assert result.audios is not None
    assert result.audios[0].mime_type == "audio/pcm"
    assert result.audios[0].content == b"\x01\x00"
    synthesize.assert_awaited_once()


@pytest.mark.asyncio
async def test_realtime_speech_rejects_other_formats(synthesize: AsyncMock) -> None:
    """A format the Realtime API cannot produce fails instead of returning mislabeled audio."""
    toolkit = MindRoomOpenAITools(
        api_key="sk-test",
        text_to_speech_model="gpt-realtime-2.1-mini",
        text_to_speech_format="mp3",
    )

    result = await _generate_speech(toolkit, "Read this.")

    assert result.content.startswith("Failed to generate speech:")
    assert "wav" in result.content
    assert "pcm" in result.content
    assert not result.audios
    synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_realtime_speech_failure_is_a_tool_failure(synthesize: AsyncMock) -> None:
    """A Realtime failure reads as a failed tool call, as a speech-endpoint failure does."""
    synthesize.side_effect = RuntimeError("socket closed")
    toolkit = MindRoomOpenAITools(api_key="sk-test", text_to_speech_model="gpt-realtime-2.1-mini")

    result = await _generate_speech(toolkit, "Read this.")

    assert result.content == "Failed to generate speech: socket closed"
    assert not result.audios


@pytest.mark.asyncio
async def test_non_realtime_model_keeps_speech_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    synthesize: AsyncMock,
) -> None:
    """A speech-endpoint model still goes through ``/v1/audio/speech``."""
    calls: list[dict[str, object]] = []

    def create(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(content=b"mp3-bytes")

    provider = SimpleNamespace(audio=SimpleNamespace(speech=SimpleNamespace(create=create)))
    monkeypatch.setattr(agno_openai, "OpenAIClient", lambda **_kwargs: provider)
    toolkit = MindRoomOpenAITools(
        api_key="sk-test",
        text_to_speech_model="gpt-4o-mini-tts",
        text_to_speech_format="mp3",
    )

    result = await _generate_speech(toolkit, "Read this.")

    assert calls == [{"model": "gpt-4o-mini-tts", "voice": "alloy", "input": "Read this.", "response_format": "mp3"}]
    assert result.audios is not None
    assert result.audios[0].content == b"mp3-bytes"
    synthesize.assert_not_awaited()


def test_disabled_speech_offers_no_generate_speech() -> None:
    """Turning speech off still removes the function from the toolkit."""
    toolkit = MindRoomOpenAITools(
        api_key="sk-test",
        enable_speech_generation=False,
        text_to_speech_model="gpt-realtime-2.1-mini",
    )

    assert "generate_speech" not in toolkit.get_async_functions()
