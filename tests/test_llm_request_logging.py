"""The request-log settings and the per-request context callers bind around a run."""

from __future__ import annotations

import pytest

from mindroom.config.main import Config
from mindroom.config.models import DebugConfig
from mindroom.constants import MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY
from mindroom.llm_request_logging import (
    build_llm_request_log_context,
    current_llm_request_log_context,
    stream_with_llm_request_log_context,
)


def test_debug_config_parses() -> None:
    """Debug config should parse both explicit and default request logging settings."""
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "debug": {"log_llm_requests": True, "llm_request_log_dir": "custom-logs"},
        },
    )
    assert config.debug == DebugConfig(log_llm_requests=True, llm_request_log_dir="custom-logs")
    assert (
        Config.model_validate({"models": {"default": {"provider": "openai", "id": "test-model"}}}).debug
        == DebugConfig()
    )


def test_request_context_names_each_source_event_once() -> None:
    """A coalesced turn lists the reply target first, then each other source event and its prompt."""
    context = build_llm_request_log_context(
        agent_id="assistant",
        session_id="session-1",
        room_id="!room:localhost",
        thread_id=None,
        reply_to_event_id="$reply",
        requester_id="@user:localhost",
        correlation_id="corr-1",
        prompt="Hi",
        model_prompt=None,
        full_prompt="System. Hi",
        metadata={
            MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$first", "$reply", "", 7, "$first"],
            MATRIX_SOURCE_EVENT_PROMPTS_METADATA_KEY: {"$first": "First", "$reply": "Hi", "": "dropped", "$bad": 1},
        },
    )

    assert context == {
        "agent_id": "assistant",
        "session_id": "session-1",
        "correlation_id": "corr-1",
        "current_turn_prompt": "Hi",
        "full_prompt": "System. Hi",
        "room_id": "!room:localhost",
        "reply_to_event_id": "$reply",
        "requester_id": "@user:localhost",
        "source_event_ids": ["$reply", "$first"],
        "source_event_prompts": {"$first": "First", "$reply": "Hi"},
    }


class _PlainAsyncIterator:
    """Async iterator without aclose(), valid under the AsyncIterator contract."""

    def __init__(self, values: list[str]) -> None:
        self._values = values
        self.contexts: list[dict[str, object]] = []

    def __aiter__(self) -> _PlainAsyncIterator:
        return self

    async def __anext__(self) -> str:
        if not self._values:
            raise StopAsyncIteration
        self.contexts.append(current_llm_request_log_context())
        return self._values.pop(0)


@pytest.mark.asyncio
async def test_stream_with_llm_request_log_context_accepts_plain_async_iterator() -> None:
    """Request-log stream binding should not require an aclose method."""
    source = _PlainAsyncIterator(["one", "two"])

    items = [
        item async for item in stream_with_llm_request_log_context(source, request_context={"correlation_id": "corr-1"})
    ]

    assert items == ["one", "two"]
    assert source.contexts == [{"correlation_id": "corr-1"}, {"correlation_id": "corr-1"}]
