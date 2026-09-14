"""Portable OpenAI summaries must finish completely within the engine's attempt count."""

from __future__ import annotations

import json
from typing import Any, Literal

import httpx
import pytest
from agno.exceptions import ModelProviderError
from openai import AsyncOpenAI

from mindroom.history.summary_call import (
    CompactionSummaryIncompleteError,
    CompactionSummaryOutputLimitError,
    generate_compaction_summary,
)
from mindroom.openai_models import MindRoomOpenAIChat, MindRoomOpenAIResponses

pytestmark = pytest.mark.asyncio

type _Route = Literal["chat", "responses"]


def _model(
    route: _Route,
    http_client: httpx.AsyncClient,
) -> MindRoomOpenAIChat | MindRoomOpenAIResponses:
    params = {"id": "gpt-6-astra", "api_key": "test-key", "base_url": "https://mock.invalid/v1"}
    if route == "chat":
        return MindRoomOpenAIChat(**params, http_client=http_client, max_completion_tokens=4)
    return MindRoomOpenAIResponses(**params, http_client=http_client, max_output_tokens=4, store=False)


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
async def test_summary_rejects_explicit_output_limit_below_usage_cap(route: _Route) -> None:
    """Completion metadata must prevent committing partial text even below the configured cap."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(route)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        model = _model(route, http_client)
        with pytest.raises(CompactionSummaryOutputLimitError):
            await generate_compaction_summary(
                model=model,
                summary_input="Conversation",
                summary_prompt="Summarize",
                timeout_seconds=10,
            )
    assert len(requests) == 1
    assert requests[0]["max_completion_tokens" if route == "chat" else "max_output_tokens"] == 4


@pytest.mark.parametrize("route", ["chat", "responses"])
async def test_summary_rejects_partial_text_stopped_by_content_filter(route: _Route) -> None:
    """Partial nonempty content is still incomplete when a provider stops for another reason."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: _response(route, reason="content_filter")),
    ) as client:
        with pytest.raises(CompactionSummaryIncompleteError, match="incomplete"):
            await generate_compaction_summary(
                model=_model(route, client),
                summary_input="Conversation",
                summary_prompt="Summarize",
                timeout_seconds=10,
            )


@pytest.mark.parametrize("route", ["chat", "responses"])
async def test_summary_accepts_explicit_completion_at_usage_cap(route: _Route) -> None:
    """A complete response is valid even when its usage equals the configured cap."""
    transport = httpx.MockTransport(lambda _: _response(route, complete=True, output_tokens=4))
    async with httpx.AsyncClient(transport=transport) as client:
        summary = await generate_compaction_summary(
            model=_model(route, client),
            summary_input="Conversation",
            summary_prompt="Summarize",
            timeout_seconds=10,
        )
    assert summary.summary == "Project facts"


@pytest.mark.parametrize("route", ["chat", "responses"])
@pytest.mark.parametrize("retry_source", ["model", "client_params", "injected"])
async def test_summary_makes_one_http_attempt_and_preserves_caller_client(
    route: _Route,
    retry_source: str,
) -> None:
    """SDK retry settings cannot multiply a summary attempt or mutate an injected client."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            503,
            headers={"retry-after-ms": "1"},
            json={"error": {"message": "Unavailable", "type": "server_error", "code": "unavailable"}},
        )

    authored_params = {"max_retries": 2}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        original_client = AsyncOpenAI(api_key="test-key", http_client=http_client, max_retries=2)
        model = _model(route, http_client)
        model.retries = 2
        if retry_source == "model":
            model.max_retries = 2
        elif retry_source == "client_params":
            model.client_params = authored_params
        else:
            model.async_client = original_client
        with pytest.raises(ModelProviderError):
            await generate_compaction_summary(
                model=model,
                summary_input="Conversation",
                summary_prompt="Summarize",
                timeout_seconds=10,
            )
        assert original_client.max_retries == 2
        assert not original_client.is_closed()
        assert authored_params == {"max_retries": 2}
    assert len(requests) == 1
