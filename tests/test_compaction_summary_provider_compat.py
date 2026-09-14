"""Summary safety follows the effective provider request, including raw overrides."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.exceptions import ModelProviderError
from anthropic import AsyncAnthropic, AsyncAnthropicBedrockMantle, AsyncAnthropicVertex
from google.oauth2.credentials import Credentials

from mindroom.anthropic_claude import MindRoomAnthropicClaude
from mindroom.bedrock_claude import MindRoomBedrockClaude
from mindroom.error_handling import ModelSafeguardRefusalError
from mindroom.history.summary_call import (
    CompactionSummaryIncompleteError,
    CompactionSummaryOutputLimitError,
    generate_compaction_summary,
)
from mindroom.vertex_claude_compat import MindroomVertexAIClaude

if TYPE_CHECKING:
    from agno.models.anthropic import Claude


pytestmark = pytest.mark.asyncio


def _model(provider: str, transport: httpx.MockTransport, request_params: dict[str, Any]) -> Claude:
    http_client = httpx.AsyncClient(transport=transport)
    if provider == "direct":
        client = AsyncAnthropic(api_key="test", http_client=http_client, max_retries=0)
        return MindRoomAnthropicClaude(
            id="claude-sonnet-5",
            max_tokens=8192,
            request_params=request_params,
            async_client=client,
        )
    if provider == "mantle":
        mantle_client = AsyncAnthropicBedrockMantle(
            aws_region="us-east-1",
            skip_auth=True,
            http_client=http_client,
            max_retries=0,
        )
        return MindRoomBedrockClaude(
            id="claude-sonnet-5",
            max_tokens=8192,
            request_params=request_params,
            async_client=mantle_client,
        )
    client = AsyncAnthropicVertex(
        project_id="demo-project",
        region="global",
        credentials=Credentials(token="test"),  # noqa: S106 - mock transport only
        http_client=http_client,
        max_retries=0,
    )
    return MindroomVertexAIClaude(
        id="claude-sonnet-5",
        max_tokens=8192,
        request_params=request_params,
        async_client=client,
    )


def _response(*, output_tokens: int, stop_reason: str = "end_turn") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_summary",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "Project facts"}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 3000, "output_tokens": output_tokens},
        },
    )


@pytest.mark.parametrize("provider", ["direct", "vertex", "mantle"])
@pytest.mark.parametrize("raw_body", [False, True])
@pytest.mark.parametrize("output_tokens", [100, 1024])
async def test_summary_rejects_output_capped_at_effective_request_limit(
    provider: str,
    output_tokens: int,
    *,
    raw_body: bool,
) -> None:
    """A lower authored wire cap must never turn a truncated summary into a durable success."""
    params = {"extra_body": {"max_tokens": 1024}} if raw_body else {"max_tokens": 1024}
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(output_tokens=output_tokens, stop_reason="max_tokens")

    model = _model(provider, httpx.MockTransport(respond), params)
    try:
        with pytest.raises(CompactionSummaryOutputLimitError):
            await generate_compaction_summary(
                model=model,
                summary_input="Conversation",
                summary_prompt="Summarize",
                timeout_seconds=10,
            )
    finally:
        await model.async_client.close()
    assert requests[0]["max_tokens"] == 1024
    assert params == ({"extra_body": {"max_tokens": 1024}} if raw_body else {"max_tokens": 1024})


@pytest.mark.parametrize("provider", ["direct", "vertex", "mantle"])
@pytest.mark.parametrize("raw_body", [False, True])
async def test_summary_disables_thinking_from_request_overrides(provider: str, *, raw_body: bool) -> None:
    """The effective body must obey summary tuning without mutating the caller's mappings."""
    thinking = {"type": "adaptive"}
    authored = {"thinking": thinking, "max_tokens": 1024, "metadata": {"user_id": "summary-user"}}
    params = {"extra_body": authored} if raw_body else authored
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(output_tokens=100)

    model = _model(provider, httpx.MockTransport(respond), params)
    try:
        result = await generate_compaction_summary(
            model=model,
            summary_input="Conversation",
            summary_prompt="Summarize",
            timeout_seconds=10,
        )
    finally:
        await model.async_client.close()
    assert result.summary == "Project facts"
    assert "thinking" not in requests[0]
    assert requests[0]["metadata"] == {"user_id": "summary-user"}
    assert authored["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize("provider", ["direct", "vertex", "mantle"])
async def test_summary_enforces_shorter_timeout_without_changing_injected_client(provider: str) -> None:
    """Request-level timeout overrides cannot escape the summary's stricter provider limit."""
    timeouts: list[dict[str, float]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return _response(output_tokens=100)

    model = _model(provider, httpx.MockTransport(respond), {"timeout": 9.0})
    model.timeout = 3.0
    original_client = model.async_client
    original_timeout = original_client.timeout
    try:
        await generate_compaction_summary(
            model=model,
            summary_input="Conversation",
            summary_prompt="Summarize",
            timeout_seconds=10,
        )
    finally:
        await model.async_client.close()
    assert timeouts[0]["read"] == 3.0
    assert original_client.timeout == original_timeout


@pytest.mark.parametrize("provider", ["direct", "vertex", "mantle"])
async def test_summary_single_attempt_disables_nested_sdk_and_model_retries(provider: str) -> None:
    """The outer compaction retry policy owns retries even with an injected SDK client."""
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"type": "error", "error": {"type": "api_error", "message": "unavailable"}})

    model = _model(provider, httpx.MockTransport(respond), {})
    model.retries = 1
    original_client = model.async_client
    original_client.max_retries = 1
    try:
        with pytest.raises(ModelProviderError):
            await generate_compaction_summary(
                model=model,
                summary_input="Conversation",
                summary_prompt="Summarize",
                timeout_seconds=10,
            )
    finally:
        await model.async_client.close()
    assert len(requests) == 1
    assert original_client.max_retries == 1


@pytest.mark.parametrize("transport_field", ["http_client", "client_params"])
async def test_summary_preserves_lazy_transport_phase_timeouts(transport_field: str) -> None:
    """An unbuilt SDK client must retain the same short transport limits as an injected one."""
    timeouts = []

    def respond(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return _response(output_tokens=100)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        timeout=httpx.Timeout(2, connect=0.5),
    ) as client:
        model = MindRoomAnthropicClaude(id="claude-sonnet-5", api_key="test")
        if transport_field == "http_client":
            model.http_client = client
        else:
            model.client_params = {"http_client": client}
        result = await generate_compaction_summary(
            model=model,
            summary_input="Conversation",
            summary_prompt="Summarize",
            timeout_seconds=10,
        )
        assert result.summary == "Project facts"
        assert timeouts == [{"connect": 0.5, "read": 2, "write": 2, "pool": 2}]


@pytest.mark.parametrize("provider", ["direct", "vertex", "mantle"])
@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence", "model_context_window_exceeded"])
async def test_summary_uses_stop_reason_and_raw_body_precedence(provider: str, stop_reason: str) -> None:
    """A normal stop at the cap is complete; a context stop below it is incomplete."""
    requests = []
    params = {"max_tokens": 4096, "extra_body": {"max_tokens": 1024}}

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(
            output_tokens=1024 if stop_reason in {"end_turn", "stop_sequence"} else 100,
            stop_reason=stop_reason,
        )

    model = _model(provider, httpx.MockTransport(respond), params)
    try:
        if stop_reason in {"end_turn", "stop_sequence"}:
            result = await generate_compaction_summary(
                model=model,
                summary_input="Conversation",
                summary_prompt="Summarize",
                timeout_seconds=10,
            )
            assert result.summary == "Project facts"
        else:
            with pytest.raises(CompactionSummaryOutputLimitError):
                await generate_compaction_summary(
                    model=model,
                    summary_input="Conversation",
                    summary_prompt="Summarize",
                    timeout_seconds=10,
                )
    finally:
        await model.async_client.close()
    assert requests[0]["max_tokens"] == 1024
    assert params == {"max_tokens": 4096, "extra_body": {"max_tokens": 1024}}


@pytest.mark.parametrize("provider", ["direct", "vertex", "mantle"])
@pytest.mark.parametrize(
    ("stop_reason", "expected_error"),
    [
        ("pause_turn", CompactionSummaryIncompleteError),
        ("tool_use", CompactionSummaryIncompleteError),
        ("unexpected", CompactionSummaryIncompleteError),
        ("refusal", ModelSafeguardRefusalError),
    ],
)
async def test_summary_rejects_unfinished_or_refused_text(
    provider: str,
    stop_reason: str,
    expected_error: type[Exception],
) -> None:
    """Partial text is not a complete summary; provider refusals retain their own error."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(output_tokens=100, stop_reason=stop_reason)

    model = _model(provider, httpx.MockTransport(respond), {})
    try:
        with pytest.raises(expected_error):
            await generate_compaction_summary(
                model=model,
                summary_input="Conversation",
                summary_prompt="Summarize",
                timeout_seconds=10,
            )
    finally:
        await model.async_client.close()
    assert len(requests) == 1
