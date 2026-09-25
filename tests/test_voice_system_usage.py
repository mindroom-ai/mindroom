"""Voice provider calls retain content-free system usage."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.metrics import MessageMetrics, RunMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput

from mindroom import helper_usage, voice_handler
from mindroom.config.main import Config
from mindroom.config.voice import VoiceConfig, VoiceSTTConfig
from mindroom.usage_stats import collect_admin_usage
from tests.conftest import test_runtime_paths
from tests.identity_helpers import persist_actual_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _stored_usage(paths: RuntimePaths) -> list[dict[str, object]]:
    with sqlite3.connect(paths.storage_root / "system/sessions/system.db") as connection:
        return [json.loads(row[0]) for row in connection.execute("SELECT usage_data FROM system_sessions_usage")]


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_failure", [False, True])
async def test_transcription_records_one_provider_request_without_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    storage_failure: bool,
) -> None:
    """A token-metered STT response creates one request; audio is a subset of input."""
    paths = test_runtime_paths(tmp_path)
    config = Config(voice=VoiceConfig(enabled=True, stt=VoiceSTTConfig(api_key="test-key", model="gpt-transcribe")))
    client_type = httpx.AsyncClient

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "text": " private transcript ",
                "usage": {
                    "type": "tokens",
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "total_tokens": 15,
                    "input_token_details": {"audio_tokens": 9, "text_tokens": 3},
                },
            },
        )

    monkeypatch.setattr(
        voice_handler.httpx,
        "AsyncClient",
        lambda *, timeout: client_type(transport=httpx.MockTransport(respond), timeout=timeout),
    )
    if storage_failure:

        async def fail_write(*_args: object, **_kwargs: object) -> None:
            raise OSError

        monkeypatch.setattr(helper_usage, "run_session_storage_operation", fail_write)
    before = time.time()
    assert await voice_handler._transcribe_audio(b"private audio", config, paths) == "private transcript"
    after = time.time()
    if storage_failure:
        return

    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.input_tokens == 12
    assert report.totals.output_tokens == 3
    assert report.totals.total_tokens == 15
    assert report.totals.audio_input_tokens == 9
    assert report.totals.audio_total_tokens == 9
    assert report.session_count == 0
    assert all(row.run_count == 0 for row in report.breakdown)
    assert len(report.request_breakdown) == 1
    request = report.request_breakdown[0]
    assert request.kind == "voice_transcription"
    assert request.model_provider == "openai"
    assert request.model == "gpt-transcribe"
    assert int(before) <= request.created_at <= int(after)
    stored = _stored_usage(paths)
    assert len(stored) == 1
    assert "private transcript" not in json.dumps(stored)
    assert "private audio" not in json.dumps(stored)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [None, {"type": "duration", "seconds": 2.5}, {"type": "tokens", "input_tokens": "bad"}],
)
async def test_transcription_preserves_text_without_valid_token_usage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    usage: dict[str, object] | None,
) -> None:
    """Absent, duration, and malformed counters do not fabricate token records or discard text."""
    paths = test_runtime_paths(tmp_path)
    config = Config(voice=VoiceConfig(enabled=True, stt=VoiceSTTConfig(api_key="test-key")))
    client_type = httpx.AsyncClient
    payload: dict[str, object] = {"text": " transcript "}
    if usage is not None:
        payload["usage"] = usage
    monkeypatch.setattr(
        voice_handler.httpx,
        "AsyncClient",
        lambda *, timeout: client_type(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=payload)),
            timeout=timeout,
        ),
    )

    assert await voice_handler._transcribe_audio(b"audio", config, paths) == "transcript"
    assert collect_admin_usage(config=config, runtime_paths=paths).totals.total_tokens == 0


@pytest.mark.asyncio
async def test_custom_stt_uses_submitted_model_without_guessing_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A compatible endpoint has a known submitted model but no reliable provider identity."""
    paths = test_runtime_paths(tmp_path)
    stt = VoiceSTTConfig(
        provider="openai_compatible",
        host="http://stt.example.test",
        model="configured-model",
    )
    # Runtime update models the existing form-data merge path, including an override.
    stt = stt.model_copy(update={"extra_kwargs": {"model": "submitted-model"}})
    config = Config(voice=VoiceConfig(enabled=True, stt=stt))
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        voice_handler.httpx,
        "AsyncClient",
        lambda *, timeout: client_type(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "text": "transcript",
                        "usage": {"type": "tokens", "input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
                    },
                ),
            ),
            timeout=timeout,
        ),
    )

    assert await voice_handler._transcribe_audio(b"audio", config, paths) == "transcript"
    stored = _stored_usage(paths)
    assert len(stored) == 1
    assert stored[0]["model"] == "submitted-model"
    assert stored[0].get("model_provider") is None
    assert "audio_input_tokens" not in stored[0]["metrics"]
    assert "audio_total_tokens" not in stored[0]["metrics"]
    assert "audio_input_tokens" not in stored[0]["requests"][0]["metrics"]
    assert "audio_total_tokens" not in stored[0]["requests"][0]["metrics"]
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 5
    assert report.request_breakdown == ()
    assert report.request_coverage is not None
    assert report.request_coverage.unavailable_sources == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_failure", [False, True])
async def test_normalizer_records_usage_before_rejecting_empty_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    storage_failure: bool,
) -> None:
    """Retain malformed usage and preserve valid normalized text when storage fails."""
    paths = test_runtime_paths(tmp_path)
    config = Config(voice=VoiceConfig(enabled=True))
    persist_actual_entity_accounts(config, paths, password="test-password")  # noqa: S106 - local fixture account
    response = RunOutput(
        run_id="voice-normalizer-paid",
        model="normalizer-model",
        model_provider="test-provider",
        content="normalized transcript" if storage_failure else None,
        metrics=RunMetrics(input_tokens=7, output_tokens=2, total_tokens=9),
        messages=[
            Message(
                role="assistant",
                created_at=1_700_000_000,
                metrics=MessageMetrics(input_tokens=7, output_tokens=2, total_tokens=9),
            ),
        ],
    )

    class FakeAgent:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def arun(self, *_args: object, **_kwargs: object) -> RunOutput:
            return response

    monkeypatch.setattr(voice_handler, "Agent", FakeAgent)
    monkeypatch.setattr(voice_handler.model_loading, "get_model_instance", lambda *_args: object())
    if storage_failure:

        async def fail_write(*_args: object, **_kwargs: object) -> None:
            raise OSError

        monkeypatch.setattr(helper_usage, "run_session_storage_operation", fail_write)

    result = await voice_handler._process_transcription("fallback transcript", config, paths)
    if storage_failure:
        assert result == "normalized transcript"
        return
    assert result == "fallback transcript"
    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 9
    assert all(row.run_count == 0 for row in report.breakdown)
    assert [row.kind for row in report.request_breakdown] == ["voice_normalization"]
    assert "fallback transcript" not in json.dumps(_stored_usage(paths))
