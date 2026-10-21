"""Portable OpenAI summaries must finish completely within the engine's attempt count."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

import httpx
import pytest

from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.history.summary_call import (
    CompactionSummaryIncompleteError,
    CompactionSummaryOutputLimitError,
    generate_compaction_summary,
)
from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, complete
from mindroom.providers.openai.build import ProviderRuntime, build_provider
from tests.providers.golden_support import pipeline_provider

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider

pytestmark = pytest.mark.asyncio

type _Route = Literal["chat", "responses"]


def _model(
    route: _Route,
    root: Path,
    respond: Callable[[httpx.Request], httpx.Response],
    **extra_kwargs: Any,  # noqa: ANN401
) -> ModelProvider:
    """Build the route's owned adapter inside the request pipeline, answering every request with ``respond``."""
    output_limit = {"max_completion_tokens": 4} if route == "chat" else {"max_output_tokens": 4, "store": False}
    config = ModelConfig(
        provider="openai",
        id="gpt-6-astra",
        api="chat_completions" if route == "chat" else "responses",
        extra_kwargs={"api_key": "test-key", "base_url": "https://mock.invalid/v1", **output_limit, **extra_kwargs},
    )
    runtime = ProviderRuntime(
        runtime_paths=resolve_runtime_paths(
            config_path=root / "config.yaml",
            storage_path=root / "data",
            process_env={},
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    return pipeline_provider(build_provider(config, runtime))


def _response(
    route: _Route,
    *,
    complete: bool = False,
    reason: str = "length",
    output_tokens: int = 1,
) -> httpx.Response:
    if route == "chat":
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-summary",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-6-astra",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop" if complete else reason,
                        "message": {"role": "assistant", "content": "Project facts"},
                    },
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": output_tokens, "total_tokens": 10 + output_tokens},
            },
        )
    return httpx.Response(
        200,
        json={
            "id": "resp_summary",
            "object": "response",
            "created_at": 1,
            "model": "gpt-6-astra",
            "status": "completed" if complete else "incomplete",
            "incomplete_details": None
            if complete
            else {"reason": "max_output_tokens" if reason == "length" else reason},
            "error": None,
            "output": [
                {
                    "id": "msg_summary",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed" if complete else "incomplete",
                    "content": [{"type": "output_text", "text": "Project facts", "annotations": []}],
                },
            ],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": 10,
                "output_tokens": output_tokens,
                "total_tokens": 10 + output_tokens,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    )


@pytest.mark.parametrize("route", ["chat", "responses"])
async def test_summary_rejects_explicit_output_limit_below_usage_cap(route: _Route, tmp_path: Path) -> None:
    """Completion metadata must prevent committing partial text even below the configured cap."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(route)

    with pytest.raises(CompactionSummaryOutputLimitError):
        await generate_compaction_summary(
            model=_model(route, tmp_path, respond),
            summary_input="Conversation",
            summary_prompt="Summarize",
            timeout_seconds=10,
        )
    assert len(requests) == 1
    assert requests[0]["max_completion_tokens" if route == "chat" else "max_output_tokens"] == 4
    assert requests[0].get("stream") is not True


@pytest.mark.parametrize("route", ["chat", "responses"])
async def test_summary_rejects_partial_text_stopped_by_content_filter(route: _Route, tmp_path: Path) -> None:
    """Partial nonempty content is still incomplete when a provider stops for another reason."""
    with pytest.raises(CompactionSummaryIncompleteError, match="incomplete"):
        await generate_compaction_summary(
            model=_model(route, tmp_path, lambda _: _response(route, reason="content_filter")),
            summary_input="Conversation",
            summary_prompt="Summarize",
            timeout_seconds=10,
        )


@pytest.mark.parametrize("route", ["chat", "responses"])
async def test_summary_accepts_explicit_completion_at_usage_cap(route: _Route, tmp_path: Path) -> None:
    """A complete response is valid even when its usage equals the configured cap."""
    summary = await generate_compaction_summary(
        model=_model(route, tmp_path, lambda _: _response(route, complete=True, output_tokens=4)),
        summary_input="Conversation",
        summary_prompt="Summarize",
        timeout_seconds=10,
    )
    assert summary == "Project facts"


@pytest.mark.parametrize("route", ["chat", "responses"])
@pytest.mark.parametrize("retry_source", ["max_retries", "client_params"])
async def test_summary_makes_one_http_attempt_whatever_retries_the_model_authors(
    route: _Route,
    retry_source: str,
    tmp_path: Path,
) -> None:
    """SDK retry settings the model authors cannot multiply a summary attempt."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            503,
            headers={"retry-after-ms": "1"},
            json={"error": {"message": "Unavailable", "type": "server_error", "code": "unavailable"}},
        )

    authored = {"max_retries": 2} if retry_source == "max_retries" else {"client_params": {"max_retries": 2}}
    with pytest.raises(ProviderError):
        await generate_compaction_summary(
            model=_model(route, tmp_path, respond, **authored),
            summary_input="Conversation",
            summary_prompt="Summarize",
            timeout_seconds=10,
        )
    assert len(requests) == 1


async def test_openrouter_claude_summary_writes_no_prompt_cache(tmp_path: Path) -> None:
    """An OpenRouter route to Claude marks cache breakpoints on replies, but never on a one-off summary."""
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        response = _response("chat", complete=True)
        return httpx.Response(200, json={**json.loads(response.content), "model": "anthropic/claude-sonnet-5"})

    config = ModelConfig(
        provider="openrouter",
        id="anthropic/claude-sonnet-5",
        extra_kwargs={"api_key": "test-key", "cache_system_prompt": True, "max_retries": 2},
    )
    runtime = ProviderRuntime(
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "data",
            process_env={},
        ),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    model = pipeline_provider(build_provider(config, runtime))
    messages = (Message(role="system", content="Summarize"), Message(role="user", content="Conversation"))

    await complete(model, ModelRequest(messages=messages, stream=False))
    summary = await generate_compaction_summary(
        model=model,
        summary_input="Conversation",
        summary_prompt="Summarize",
        timeout_seconds=10,
    )

    assert summary == "Project facts"
    reply_body, summary_body = bodies
    assert "cache_control" in json.dumps(reply_body)
    assert "cache_control" not in json.dumps(summary_body)
