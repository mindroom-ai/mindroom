"""Provider overload recovery through the real OpenAI-compatible streaming path."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.agent import Agent
from agno.exceptions import ModelProviderError
from agno.media import Image
from agno.models.message import Message
from agno.run.agent import RunCompletedEvent, RunErrorEvent
from openai import AsyncOpenAI

from mindroom import provider_stream_retry
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.model_loading import get_model_instance
from mindroom.openai_models import MindRoomOpenAIChat
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class _Provider:
    attempts: list[str | httpx.Response]
    requests: list[dict[str, object]] = field(default_factory=list)
    responses: list[httpx.Response] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        attempt = self.attempts.pop(0)
        response = (
            attempt
            if isinstance(attempt, httpx.Response)
            else httpx.Response(200, headers={"content-type": "text/event-stream"}, content=attempt)
        )
        self.responses.append(response)
        return response


def _overload() -> str:
    return (
        "data: "
        + json.dumps(
            {
                "error": {
                    "message": "litellm.APIError: Our servers are currently overloaded. Please try again later.",
                    "type": "api_error",
                    "code": "500",
                },
            },
        )
        + "\n\n"
    )


def _chunk(delta: dict[str, object], finish_reason: str | None = None) -> str:
    return (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "test-model",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            },
        )
        + "\n\n"
    )


def _answer(content: str) -> str:
    return _chunk({"content": content}) + _chunk({}, "stop") + "data: [DONE]\n\n"


@asynccontextmanager
async def _model(provider: _Provider, tmp_path: Path) -> AsyncIterator[MindRoomOpenAIChat]:
    config = bind_runtime_paths(
        Config(models={"default": ModelConfig(provider="openai", id="test-model", api_key="test-key")}),
        test_runtime_paths(tmp_path),
    )
    model = get_model_instance(config, runtime_paths_for(config))
    assert isinstance(model, MindRoomOpenAIChat)
    async with AsyncOpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.respond)),
    ) as client:
        model.async_client = client
        yield model


@pytest.fixture
def retry_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record requested backoffs without waiting for the wall clock."""
    delays: list[float] = []

    async def wait(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(provider_stream_retry.asyncio, "sleep", wait)
    return delays


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        _overload(),
        httpx.Response(502, json={"error": {"message": "Upstream unavailable"}}),
        'data: {"error":{"type":"overloaded_error","message":"Temporarily unavailable"}}\n\n',
        'data: {"error":{"type":"api_error","message":"Our servers are currently overloaded"}}\n\n',
    ],
    ids=["proxy-code", "http-502", "overloaded-type", "api-overload"],
)
async def test_proxy_overload_retries_after_delay(
    failure: str | httpx.Response,
    tmp_path: Path,
    retry_delays: list[float],
) -> None:
    """An SDK overload after HTTP success retries the same provider request."""
    provider = _Provider([failure, _answer("Recovered")])
    async with _model(provider, tmp_path) as model:
        chunks = [
            chunk
            async for chunk in model.ainvoke_stream([Message(role="user", content="Hello")], Message(role="assistant"))
        ]

    assert "".join(chunk.content or "" for chunk in chunks) == "Recovered"
    assert len(provider.requests) == 2
    assert provider.requests[0] == provider.requests[1]
    assert len(retry_delays) == 1
    assert 1 <= retry_delays[0] <= 1.25
    assert all(response.is_closed for response in provider.responses)


@pytest.mark.asyncio
async def test_proxy_overload_retry_budget_is_bounded(tmp_path: Path, retry_delays: list[float]) -> None:
    """Persistent overload exhausts four delayed retries without another request."""
    provider = _Provider([_overload() for _ in range(6)])
    async with _model(provider, tmp_path) as model:
        with pytest.raises(ModelProviderError, match="overloaded"):
            _ = [chunk async for chunk in model.ainvoke_stream([], Message(role="assistant"))]

    assert len(provider.requests) == 5
    assert len(retry_delays) == 4
    assert all(base <= delay <= base * 1.25 for base, delay in zip((1, 2, 4, 8), retry_delays, strict=True))
    assert all(response.is_closed for response in provider.responses)


@pytest.mark.asyncio
async def test_media_does_not_restart_exhausted_provider_retry_budget(
    tmp_path: Path,
    retry_delays: list[float],
) -> None:
    """Provider outages retain media and exhaust one budget across both wrappers."""
    provider = _Provider([_overload() for _ in range(10)])
    messages = [Message(role="user", content="Describe", images=[Image(url="https://example.org/image.png")])]
    async with _model(provider, tmp_path) as model:
        with pytest.raises(ModelProviderError, match="overloaded"):
            _ = [chunk async for chunk in model.ainvoke_stream(messages, Message(role="assistant"))]

    assert len(provider.requests) == 5
    assert all(request == provider.requests[0] for request in provider.requests)
    assert len(retry_delays) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"type": "invalid_request_error", "code": "400", "message": "Our servers are currently overloaded"},
        {"type": "api_error", "message": "Unclassified provider error"},
        {"type": {}, "message": "Malformed provider error"},
    ],
)
async def test_unclassified_or_permanent_sse_error_is_not_retried(
    body: dict[str, object],
    tmp_path: Path,
    retry_delays: list[float],
) -> None:
    """Agno's default 502 must not turn every SDK stream error into a retry."""
    provider = _Provider(["data: " + json.dumps({"error": body}) + "\n\n", _answer("Must not run")])
    async with _model(provider, tmp_path) as model:
        with pytest.raises(ModelProviderError):
            _ = [chunk async for chunk in model.ainvoke_stream([], Message(role="assistant"))]

    assert len(provider.requests) == 1
    assert not retry_delays


@pytest.mark.asyncio
async def test_retry_delay_remains_cancellable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """STOP or supersession during backoff must never issue the next request."""
    waiting = asyncio.Event()
    release = asyncio.Event()

    async def wait(_delay: float) -> None:
        waiting.set()
        await release.wait()

    monkeypatch.setattr(provider_stream_retry.asyncio, "sleep", wait)
    provider = _Provider([_overload(), _answer("Must not run")])
    async with _model(provider, tmp_path) as model:

        async def collect() -> None:
            _ = [chunk async for chunk in model.ainvoke_stream([], Message(role="assistant"))]

        task = asyncio.create_task(collect())
        entered = asyncio.create_task(waiting.wait())
        try:
            done, _ = await asyncio.wait({task, entered}, timeout=1, return_when=asyncio.FIRST_COMPLETED)
            assert not task.done(), "provider error escaped instead of entering cancellable backoff"
            assert entered in done
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(provider.requests) == 1
            assert all(response.is_closed for response in provider.responses)
        finally:
            release.set()
            task.cancel()
            entered.cancel()
            await asyncio.gather(task, entered, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_permanent_provider_error_is_not_retried(
    status: int,
    tmp_path: Path,
    retry_delays: list[float],
) -> None:
    """Permanent SDK status codes cannot authorize replay regardless of wording."""
    provider = _Provider(
        [
            httpx.Response(status, json={"error": {"message": "Our servers are currently overloaded"}}),
            _answer("Must not run"),
        ],
    )
    async with _model(provider, tmp_path) as model:
        with pytest.raises(ModelProviderError):
            _ = [chunk async for chunk in model.ainvoke_stream([], Message(role="assistant"))]

    assert len(provider.requests) == 1
    assert not retry_delays


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delta",
    [
        {"content": "Partial"},
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_partial",
                    "type": "function",
                    "function": {"name": "get_status", "arguments": "{}"},
                },
            ],
        },
    ],
)
async def test_overload_after_output_is_not_replayed(
    delta: dict[str, object],
    tmp_path: Path,
    retry_delays: list[float],
) -> None:
    """Partial text or tool calls cannot be replayed by either retry layer."""
    provider = _Provider([_chunk(delta) + _overload(), _answer("Must not run")])
    async with _model(provider, tmp_path) as model:
        model.retries = 1
        with pytest.raises(ModelProviderError, match="overloaded"):
            _ = [
                chunk
                async for chunk in model._ainvoke_stream_with_retry(
                    messages=[],
                    assistant_message=Message(role="assistant"),
                )
            ]

    assert len(provider.requests) == 1
    assert not retry_delays


@pytest.mark.asyncio
async def test_overload_followup_reuses_completed_tool_result(tmp_path: Path, retry_delays: list[float]) -> None:
    """Retry the post-tool request without executing the completed action twice."""
    actions: list[str] = []

    def get_status() -> str:
        """Read and record the current status."""
        actions.append("read")
        return "ready"

    tool_stream = (
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_status",
                        "type": "function",
                        "function": {"name": "get_status", "arguments": "{}"},
                    },
                ],
            },
        )
        + _chunk({}, "tool_calls")
        + "data: [DONE]\n\n"
    )
    provider = _Provider([tool_stream, _overload(), _answer("Ready")])
    async with _model(provider, tmp_path) as model:
        agent = Agent(model=model, tools=[get_status])
        events = [event async for event in agent.arun("Check status", stream=True, stream_events=True)]

    assert any(isinstance(event, RunCompletedEvent) for event in events)
    assert not any(isinstance(event, RunErrorEvent) for event in events)
    assert actions == ["read"]
    assert len(provider.requests) == 3
    assert provider.requests[1] == provider.requests[2]
    assert any(
        message["role"] == "tool" and message["content"] == "ready" for message in provider.requests[2]["messages"]
    )
    assert len(retry_delays) == 1
