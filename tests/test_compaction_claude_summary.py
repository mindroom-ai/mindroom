"""Claude summaries go out as one tuned request and finish completely, on every Claude route."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from mindroom.history.summary_call import (
    CompactionSummaryIncompleteError,
    CompactionSummaryOutputLimitError,
    generate_compaction_summary,
)
from mindroom.llm.errors import ModelSafeguardRefusal, ProviderError
from tests.providers.anthropic_family_support import isolated_provider_environment, provider_for
from tests.providers.golden_support import pipeline_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider
    from mindroom.llm.usage import MessageMetrics

pytestmark = pytest.mark.asyncio

_ROUTES = {
    "anthropic": {"provider": "anthropic", "id": "claude-sonnet-5", "extra_kwargs": {"api_key": "test-key"}},
    "vertexai_claude": {
        "provider": "vertexai_claude",
        "id": "claude-sonnet-5",
        "extra_kwargs": {"project_id": "demo-project", "region": "global"},
    },
    "bedrock_claude": {
        "provider": "bedrock_claude",
        "id": "anthropic.claude-sonnet-5",
        "extra_kwargs": {"aws_access_key": "test-access", "aws_secret_key": "test-secret", "aws_region": "us-east-1"},
    },
}


@pytest.fixture(autouse=True)
def _no_provider_environment() -> Iterator[None]:
    with isolated_provider_environment():
        yield


def _model(
    route: str,
    root: Path,
    respond: Callable[[httpx.Request], httpx.Response],
    **extra_kwargs: Any,  # noqa: ANN401
) -> ModelProvider:
    """Build the route's owned Claude adapter inside the request pipeline, answering with ``respond``."""
    config = json.loads(json.dumps(_ROUTES[route]))
    config["extra_kwargs"].update(extra_kwargs)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return pipeline_provider(provider_for(config, root, http_client))


def _response(*, output_tokens: int = 100, stop_reason: str = "end_turn") -> httpx.Response:
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


async def _summarize(model: ModelProvider, *, timeout_seconds: float = 10) -> str:
    return await generate_compaction_summary(
        model=model,
        summary_input="Conversation",
        summary_prompt="Summarize",
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize("raw_body", [False, True])
async def test_summary_rejects_output_capped_at_effective_request_limit(
    route: str,
    tmp_path: Path,
    *,
    raw_body: bool,
) -> None:
    """A lower authored wire cap must never turn a truncated summary into a durable success."""
    params = {"extra_body": {"max_tokens": 1024}} if raw_body else {"max_tokens": 1024}
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(stop_reason="max_tokens")

    with pytest.raises(CompactionSummaryOutputLimitError):
        await _summarize(_model(route, tmp_path, respond, request_params=params))
    assert requests[0]["max_tokens"] == 1024
    assert "stream" not in requests[0]


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize("raw_body", [False, True])
async def test_summary_disables_thinking_and_the_prompt_cache(route: str, tmp_path: Path, *, raw_body: bool) -> None:
    """Thinking authored anywhere is off, other authored fields stay, and the system prompt carries no cache marker."""
    authored = {"thinking": {"type": "adaptive"}, "max_tokens": 1024, "metadata": {"user_id": "summary-user"}}
    params = {"extra_body": authored} if raw_body else authored
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response()

    model = _model(
        route,
        tmp_path,
        respond,
        request_params=params,
        thinking={"type": "enabled", "budget_tokens": 2048},
        cache_system_prompt=True,
    )

    assert await _summarize(model) == "Project facts"
    [body] = requests
    assert "thinking" not in body
    assert body["metadata"] == {"user_id": "summary-user"}
    assert body["system"] == [{"type": "text", "text": "Summarize"}]
    assert authored["thinking"] == {"type": "adaptive"}


@pytest.mark.parametrize("route", sorted(_ROUTES))
async def test_summary_uses_an_authored_provider_timeout_shorter_than_the_deadline(route: str, tmp_path: Path) -> None:
    """A provider timeout the model authors still applies under a longer compaction deadline."""
    timeouts: list[dict[str, float]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"])
        return _response()

    await _summarize(_model(route, tmp_path, respond, timeout=3.0), timeout_seconds=600)

    assert timeouts[0]["read"] == 3.0


@pytest.mark.parametrize("route", sorted(_ROUTES))
async def test_summary_single_attempt_disables_authored_sdk_retries(route: str, tmp_path: Path) -> None:
    """The outer compaction retry policy owns retries, whatever retry budget the model authors."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, json={"type": "error", "error": {"type": "api_error", "message": "unavailable"}})

    with pytest.raises(ProviderError):
        await _summarize(_model(route, tmp_path, respond, client_params={"max_retries": 2}))
    assert len(requests) == 1


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize("stop_reason", ["end_turn", "stop_sequence", "model_context_window_exceeded"])
async def test_summary_uses_stop_reason_and_raw_body_precedence(route: str, stop_reason: str, tmp_path: Path) -> None:
    """A normal stop at the cap is complete; a context stop below it is cut off."""
    requests: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _response(
            output_tokens=1024 if stop_reason in {"end_turn", "stop_sequence"} else 100,
            stop_reason=stop_reason,
        )

    model = _model(route, tmp_path, respond, request_params={"max_tokens": 4096, "extra_body": {"max_tokens": 1024}})
    if stop_reason in {"end_turn", "stop_sequence"}:
        assert await _summarize(model) == "Project facts"
    else:
        with pytest.raises(CompactionSummaryOutputLimitError):
            await _summarize(model)
    assert requests[0]["max_tokens"] == 1024


@pytest.mark.parametrize("route", sorted(_ROUTES))
@pytest.mark.parametrize(
    ("stop_reason", "expected_error"),
    [
        ("pause_turn", CompactionSummaryIncompleteError),
        ("tool_use", CompactionSummaryIncompleteError),
        ("a_reason_claude_adds_later", CompactionSummaryIncompleteError),
        ("refusal", ModelSafeguardRefusal),
    ],
)
async def test_summary_rejects_unfinished_or_refused_text(
    route: str,
    stop_reason: str,
    expected_error: type[Exception],
    tmp_path: Path,
) -> None:
    """Partial text is not a complete summary; provider refusals retain their own error."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _response(stop_reason=stop_reason)

    with pytest.raises(expected_error):
        await _summarize(_model(route, tmp_path, respond))
    assert len(requests) == 1


@pytest.mark.parametrize("route", sorted(_ROUTES))
async def test_a_refused_summary_still_records_its_usage(route: str, tmp_path: Path) -> None:
    """The refused response was billed, so its usage counts before the refusal reaches the compaction."""
    recorded: list[MessageMetrics] = []

    async def on_usage(usage: MessageMetrics) -> None:
        recorded.append(usage)

    model = _model(route, tmp_path, lambda _request: _response(output_tokens=7, stop_reason="refusal"))
    with pytest.raises(ModelSafeguardRefusal):
        await generate_compaction_summary(
            model=model,
            summary_input="Conversation",
            summary_prompt="Summarize",
            timeout_seconds=10,
            on_usage=on_usage,
        )
    assert [(usage.input_tokens, usage.output_tokens) for usage in recorded] == [(3000, 7)]
