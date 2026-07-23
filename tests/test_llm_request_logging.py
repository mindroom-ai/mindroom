"""Tests for full LLM request logging."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from agno.models.anthropic import Claude
from agno.models.message import Message, MessageMetrics
from agno.models.response import ModelResponse
from structlog.testing import capture_logs

from mindroom.claude_prompt_cache import install_claude_deferred_tool_search
from mindroom.config.main import Config
from mindroom.config.models import DebugConfig
from mindroom.llm_request_logging import (
    _RequestLogRef,
    _write_llm_response_log,
    bind_llm_request_log_context,
    current_llm_request_log_context,
    install_llm_request_logging,
    record_llm_request_tools,
    stream_with_llm_request_log_context,
)
from mindroom.openai_tool_search import request_params_with_deferred_tool_search

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@dataclass
class _FakeModel:
    id: str = "test-model"
    provider: str | None = "OpenAI"
    system_prompt: str | None = None
    temperature: float | None = 0.7
    client: object | None = None
    async_client: object | None = None

    response_usage: MessageMetrics | None = None

    async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
        return ModelResponse(content="ok", response_usage=self.response_usage)

    async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
        yield ModelResponse(content="ok")
        yield ModelResponse(content="!", response_usage=self.response_usage)


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


@dataclass
class _DeferredWireModel(_FakeModel):
    """Fake adapter that filters one deferred schema until load_tool."""

    deferred_tool_loaded: bool = False

    def load_tool(self) -> None:
        self.deferred_tool_loaded = True

    def _record_wire_tools(self, kwargs: dict[str, object]) -> None:
        tools = kwargs.get("tools")
        assert isinstance(tools, list)
        wire_tools = [
            tool
            for tool in tools
            if self.deferred_tool_loaded or not isinstance(tool, dict) or tool.get("name") != "deferred_search"
        ]
        record_llm_request_tools(wire_tools)

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        self._record_wire_tools(kwargs)
        return ModelResponse(content="ok")

    async def ainvoke_stream(self, *_args: object, **kwargs: object) -> AsyncIterator[ModelResponse]:
        self._record_wire_tools(kwargs)
        yield ModelResponse(content="ok")


@dataclass
class _OpenAIDeferredWireModel(_FakeModel):
    """Fake OpenAI adapter that runs MindRoom's real wire-tool preparation."""

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        request_params_with_deferred_tool_search(
            {"tools": kwargs.get("tools")},
            frozenset({"deferred_search"}),
        )
        return ModelResponse(content="ok")


def _read_log_entries(log_dir: Path) -> list[dict[str, Any]]:
    log_files = list(log_dir.glob("llm-requests-*.jsonl"))
    assert len(log_files) == 1
    return [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]


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


@pytest.mark.asyncio
async def test_llm_request_logging_writes_jsonl(tmp_path: Path) -> None:  # noqa: PLR0915
    """Enabled request logging should emit one full JSONL entry per invoke path."""
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    messages = [
        Message(role="system", content="s" * 600, created_at=111),
        Message(
            role="user",
            content="hello",
            created_at=222,
            metrics=MessageMetrics(input_tokens=2, total_tokens=2, duration=1.5),
        ),
    ]
    assistant_message = Message(role="assistant")

    with bind_llm_request_log_context(
        agent_id="assistant",
        session_id="session-123",
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
        requester_id="@user:example.com",
        correlation_id="$reply:example.com",
        current_turn_prompt="try now",
        model_prompt="try now\n\nbe explicit",
        full_prompt="system\n\nuser: try now",
        source_event_ids=["$reply:example.com", "$coalesced:example.com"],
        source_event_prompts={"$coalesced:example.com": "older prompt"},
    ):
        result = await model.ainvoke(
            messages=messages,
            assistant_message=assistant_message,
            tools=[{"name": "search"}],
        )
    assert result.content == "ok"

    with bind_llm_request_log_context(
        agent_id="assistant",
        session_id="session-123",
        room_id="!room:example.com",
        thread_id="$thread:example.com",
        reply_to_event_id="$reply:example.com",
        requester_id="@user:example.com",
        correlation_id="$reply:example.com",
        current_turn_prompt="try now",
        model_prompt="try now\n\nbe explicit",
        full_prompt="system\n\nuser: try now",
        source_event_ids=["$reply:example.com", "$coalesced:example.com"],
        source_event_prompts={"$coalesced:example.com": "older prompt"},
    ):
        stream = model.ainvoke_stream(
            messages=messages,
            assistant_message=assistant_message,
            tools=[],
        )
    streamed = [chunk async for chunk in stream]
    assert [chunk.content for chunk in streamed] == ["ok", "!"]

    entries = _read_log_entries(tmp_path)
    assert len(entries) == 2
    assert entries[0]["agent_id"] == "assistant"
    assert entries[0]["model_id"] == "test-model"
    assert entries[0]["session_id"] == "session-123"
    assert entries[0]["room_id"] == "!room:example.com"
    assert entries[0]["thread_id"] == "$thread:example.com"
    assert entries[0]["reply_to_event_id"] == "$reply:example.com"
    assert entries[0]["requester_id"] == "@user:example.com"
    assert entries[0]["correlation_id"] == "$reply:example.com"
    assert entries[0]["current_turn_prompt"] == "try now"
    assert entries[0]["model_prompt"] == "try now\n\nbe explicit"
    assert entries[0]["full_prompt"] == "system\n\nuser: try now"
    assert entries[0]["source_event_ids"] == ["$reply:example.com", "$coalesced:example.com"]
    assert entries[0]["source_event_prompts"] == {"$coalesced:example.com": "older prompt"}
    assert entries[0]["system_prompt"] == "s" * 600
    assert entries[0]["messages"][0]["role"] == "system"
    assert entries[0]["messages"][0]["content"] == "s" * 600
    assert entries[0]["messages"][0]["created_at"] == 111
    assert entries[0]["messages"][1]["role"] == "user"
    assert entries[0]["messages"][1]["content"] == "hello"
    assert entries[0]["messages"][1]["created_at"] == 222
    assert entries[0]["messages"][1]["metrics"]["input_tokens"] == 2
    assert entries[0]["messages"][1]["metrics"]["total_tokens"] == 2
    assert entries[0]["messages"][1]["metrics"]["duration"] == 1.5
    assert entries[0]["message_count"] == 2
    assert entries[0]["tools"] == [{"name": "search"}]
    assert entries[0]["tool_count"] == 1
    assert entries[0]["model_params"] == {"temperature": 0.7}
    assert "timestamp" in entries[0]
    assert entries[1]["messages"][0]["created_at"] == 111
    assert entries[1]["messages"][1]["created_at"] == 222
    assert entries[1]["messages"][1]["metrics"]["input_tokens"] == 2
    assert entries[1]["thread_id"] == "$thread:example.com"
    assert entries[1]["reply_to_event_id"] == "$reply:example.com"
    assert entries[1]["requester_id"] == "@user:example.com"
    assert entries[1]["correlation_id"] == "$reply:example.com"
    assert entries[1]["tools"] == []
    assert entries[1]["tool_count"] == 0


@pytest.mark.asyncio
async def test_llm_request_logging_uses_post_defer_wire_tools(tmp_path: Path) -> None:
    """Logs should exclude an unloaded deferred schema and include it after load_tool."""
    model = _DeferredWireModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    catalog_tools = [{"name": "always_available"}, {"name": "deferred_search"}]
    request_kwargs = {
        "messages": [Message(role="user", content="search")],
        "assistant_message": Message(role="assistant"),
        "tools": catalog_tools,
    }

    await model.ainvoke(**request_kwargs)
    model.load_tool()
    assert [chunk.content async for chunk in model.ainvoke_stream(**request_kwargs)] == ["ok"]

    before_load, after_load = _read_log_entries(tmp_path)
    assert before_load["tools"] == [{"name": "always_available"}]
    assert before_load["tool_count"] == 1
    assert after_load["tools"] == catalog_tools
    assert after_load["tool_count"] == 2


@pytest.mark.asyncio
async def test_llm_request_logging_records_claude_native_wire_tools(tmp_path: Path) -> None:
    """Claude logs should match the final native-search array passed to its SDK."""
    model = Claude(id="claude-opus-4-8", api_key="test-key", cache_system_prompt=False)

    class _FakeMessagesAPI:
        async def create(self, **_kwargs: object) -> object:
            return object()

    class _FakeClient:
        def __init__(self) -> None:
            self.messages = _FakeMessagesAPI()

    vars(model)["get_async_client"] = lambda: _FakeClient()
    vars(model)["_has_beta_features"] = lambda **_kwargs: False
    vars(model)["_parse_provider_response"] = lambda *_args, **_kwargs: ModelResponse(content="ok")
    install_llm_request_logging(
        model,
        agent_name="claude",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({"deferred_search"}))
    tools = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} description",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in ("always_available", "deferred_search")
    ]

    await model.ainvoke(
        messages=[Message(role="user", content="search")],
        assistant_message=Message(role="assistant"),
        tools=tools,
    )

    entry = _read_log_entries(tmp_path)[0]
    assert [tool["name"] for tool in entry["tools"]] == [
        "tool_search_tool_regex",
        "always_available",
        "deferred_search",
    ]
    assert entry["tools"][0]["type"] == "tool_search_tool_regex_20251119"
    assert entry["tools"][2]["defer_loading"] is True
    assert entry["tool_count"] == 3


@pytest.mark.asyncio
async def test_llm_request_logging_records_openai_native_wire_tools(tmp_path: Path) -> None:
    """OpenAI logs should match the final server-side-search tools array."""
    model = _OpenAIDeferredWireModel()
    install_llm_request_logging(
        model,
        agent_name="openai",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )
    tools = [
        {"type": "function", "name": "always_available"},
        {"type": "function", "name": "deferred_search"},
    ]

    await model.ainvoke(
        messages=[Message(role="user", content="search")],
        assistant_message=Message(role="assistant"),
        tools=tools,
    )

    entry = _read_log_entries(tmp_path)[0]
    assert entry["tools"] == [
        {"type": "tool_search"},
        {"type": "function", "name": "always_available"},
        {"type": "function", "name": "deferred_search", "defer_loading": True},
    ]
    assert entry["tool_count"] == 3


@pytest.mark.asyncio
async def test_llm_request_logging_redacts_sensitive_values_before_jsonl_write(tmp_path: Path) -> None:
    """Durable request logs should preserve structure while masking credentials."""
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with bind_llm_request_log_context(
        agent_id="assistant",
        session_id="session-123",
        correlation_id="corr-1",
        callback_url="https://example.test/oauth/callback?code=code-secret&state=state-secret&keep=1",
    ):
        await model.ainvoke(
            messages=[
                Message(
                    role="user",
                    content="call failed with Authorization: Bearer auth-secret and api_key=api-secret",
                ),
            ],
            assistant_message=Message(role="assistant"),
            tools=[
                {
                    "name": "custom_api",
                    "headers": {
                        "Authorization": "Bearer auth-secret",
                        "set-cookie": "session=secret",
                    },
                    "nested": [{"refresh_token": "refresh-secret"}],
                },
            ],
        )

    entry = _read_log_entries(tmp_path)[0]
    serialized = json.dumps(entry)

    assert "auth-secret" not in serialized
    assert "api-secret" not in serialized
    assert "refresh-secret" not in serialized
    assert "code-secret" not in serialized
    assert "state-secret" not in serialized
    assert entry["messages"][0]["content"] == (
        "call failed with Authorization: Bearer ***redacted*** and api_key=***redacted***"
    )
    assert entry["tools"][0]["headers"] == {
        "Authorization": "***redacted***",
        "set-cookie": "***redacted***",
    }
    assert entry["tools"][0]["nested"] == [{"refresh_token": "***redacted***"}]
    assert entry["callback_url"] == (
        "https://example.test/oauth/callback?code=***redacted***&state=***redacted***&keep=1"
    )


@pytest.mark.asyncio
async def test_stream_with_llm_request_log_context_accepts_plain_async_iterator() -> None:
    """Request-log stream binding should not require an aclose method."""
    source = _PlainAsyncIterator(["one", "two"])

    async def collect() -> list[str]:
        return [
            item
            async for item in stream_with_llm_request_log_context(
                source,
                request_context={"correlation_id": "corr-1"},
            )
        ]

    assert await collect() == ["one", "two"]
    assert source.contexts == [
        {"correlation_id": "corr-1"},
        {"correlation_id": "corr-1"},
    ]


@pytest.mark.asyncio
async def test_llm_request_logging_uses_model_name_when_context_is_unbound(tmp_path: Path) -> None:
    """Unbound model calls should still keep their configured model-owner attribution."""
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="router",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    await model.ainvoke(
        messages=[Message(role="user", content="route this")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )

    entries = _read_log_entries(tmp_path)
    assert entries[0]["agent_id"] == "router"
    assert entries[0]["model_id"] == "test-model"


@pytest.mark.asyncio
async def test_llm_request_logging_disabled_still_emits_usage_telemetry(tmp_path: Path) -> None:
    """Usage telemetry should stay enabled without writing full request logs."""
    model = _FakeModel(
        response_usage=MessageMetrics(
            input_tokens=1_000,
            output_tokens=50,
            reasoning_tokens=20,
            cache_read_tokens=800,
            cache_write_tokens=100,
        ),
    )
    with capture_logs() as logs:
        install_llm_request_logging(
            model,
            agent_name="default",
            debug_config=DebugConfig(),
            default_log_dir=tmp_path,
            configured_provider="openai",
        )
        with bind_llm_request_log_context(correlation_id="corr-1", full_prompt="private prompt"):
            await model.ainvoke(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )

    assert list(tmp_path.iterdir()) == []
    assert logs == [
        {
            "event": "LLM usage",
            "log_level": "info",
            "model_name": "default",
            "model_id": "test-model",
            "provider": "OpenAI",
            "usage_available": True,
            "input_tokens": 1_000,
            "context_input_tokens": 1_000,
            "output_tokens": 50,
            "reasoning_tokens": 20,
            "cache_read_tokens": 800,
            "cache_write_tokens": 100,
            "uncached_input_tokens": 200,
            "cache_read_ratio": 0.8,
            "correlation_id": "corr-1",
        },
    ]


@pytest.mark.asyncio
async def test_llm_request_log_failure_does_not_fail_invoke_or_drop_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional request persistence must not change response or usage behavior."""
    log_error = OSError("disk full")

    async def fail_request_log(**_kwargs: object) -> None:
        raise log_error

    monkeypatch.setattr(
        "mindroom.llm_request_logging._write_llm_request_log_if_enabled",
        fail_request_log,
    )
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=12, output_tokens=3))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with capture_logs() as logs:
        response = await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    assert response.content == "ok"
    assert [entry["event"] for entry in logs] == [
        "Failed to persist LLM request log",
        "LLM usage",
    ]


@pytest.mark.asyncio
async def test_llm_request_log_failure_does_not_mask_provider_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request-log failure in finally must preserve the provider exception."""
    provider_error = RuntimeError("provider failed")
    log_error = OSError("disk full")

    @dataclass
    class _FailingModel(_FakeModel):
        async def ainvoke(self, *_args: object, **_kwargs: object) -> ModelResponse:
            raise provider_error

    async def fail_request_log(**_kwargs: object) -> None:
        raise log_error

    monkeypatch.setattr(
        "mindroom.llm_request_logging._write_llm_request_log_if_enabled",
        fail_request_log,
    )
    model = _FailingModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with capture_logs(), pytest.raises(RuntimeError, match="provider failed"):
        await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )


@pytest.mark.asyncio
async def test_llm_request_log_failure_does_not_terminate_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Streaming must remain usable when optional request persistence fails."""
    log_error = OSError("disk full")

    async def fail_request_log(**_kwargs: object) -> None:
        raise log_error

    monkeypatch.setattr(
        "mindroom.llm_request_logging._write_llm_request_log_if_enabled",
        fail_request_log,
    )
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=12, output_tokens=3))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with capture_logs() as logs:
        chunks = [
            chunk
            async for chunk in model.ainvoke_stream(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )
        ]

    assert [chunk.content for chunk in chunks] == ["ok", "!"]
    assert [entry["event"] for entry in logs] == [
        "Failed to persist LLM request log",
        "LLM usage",
    ]


@pytest.mark.asyncio
async def test_llm_response_log_failure_does_not_fail_invoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional response telemetry must not replace a successful response."""
    log_error = OSError("disk full")

    async def fail_response_log(**_kwargs: object) -> None:
        raise log_error

    monkeypatch.setattr(
        "mindroom.llm_request_logging._write_llm_response_log",
        fail_response_log,
    )
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=12, output_tokens=3))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(),
        default_log_dir=tmp_path,
    )

    with capture_logs() as logs:
        response = await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    assert response.content == "ok"
    assert [entry["event"] for entry in logs] == ["Failed to emit LLM response telemetry"]


@pytest.mark.asyncio
async def test_llm_usage_telemetry_normalizes_anthropic_cache_tokens(tmp_path: Path) -> None:
    """Anthropic cache tokens should be added to raw input before calculating cache ratios."""
    model = _FakeModel(
        provider="Anthropic",
        response_usage=MessageMetrics(input_tokens=200, cache_read_tokens=800, cache_write_tokens=100),
    )
    with capture_logs() as logs:
        install_llm_request_logging(
            model,
            agent_name="claude",
            debug_config=DebugConfig(),
            default_log_dir=tmp_path,
            configured_provider="anthropic",
        )
        await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    usage_log = logs[0]
    assert usage_log["context_input_tokens"] == 1_100
    assert usage_log["uncached_input_tokens"] == 300
    assert usage_log["cache_read_ratio"] == 0.727273


@pytest.mark.asyncio
async def test_llm_usage_telemetry_reports_missing_provider_metrics(tmp_path: Path) -> None:
    """Completed calls without provider metrics should remain visible in telemetry."""
    model = _FakeModel()
    with capture_logs() as logs:
        install_llm_request_logging(
            model,
            agent_name="default",
            debug_config=DebugConfig(),
            default_log_dir=tmp_path,
        )
        with bind_llm_request_log_context(correlation_id="corr-no-usage"):
            await model.ainvoke(
                messages=[Message(role="user", content="hello")],
                assistant_message=Message(role="assistant"),
                tools=[],
            )

    assert logs == [
        {
            "event": "LLM usage",
            "log_level": "info",
            "model_name": "default",
            "model_id": "test-model",
            "provider": "OpenAI",
            "usage_available": False,
            "correlation_id": "corr-no-usage",
        },
    ]


@pytest.mark.asyncio
async def test_llm_usage_telemetry_does_not_double_count_invoke_via_stream(tmp_path: Path) -> None:
    """A model whose invoke method consumes its stream should emit one usage event."""

    @dataclass
    class _InvokeViaStreamModel(_FakeModel):
        async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
            response = ModelResponse(content="")
            async for chunk in self.ainvoke_stream(*args, **kwargs):
                response.content = f"{response.content or ''}{chunk.content or ''}"
                if chunk.response_usage is not None:
                    response.response_usage = chunk.response_usage
            return response

    model = _InvokeViaStreamModel(response_usage=MessageMetrics(input_tokens=100, cache_read_tokens=80))
    with capture_logs() as logs:
        install_llm_request_logging(
            model,
            agent_name="default",
            debug_config=DebugConfig(),
            default_log_dir=tmp_path,
        )
        response = await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    assert response.content == "ok!"
    assert [entry["event"] for entry in logs] == ["LLM usage"]
    assert logs[0]["cache_read_ratio"] == 0.8


@pytest.mark.asyncio
async def test_llm_usage_telemetry_counts_call_while_stream_is_paused(tmp_path: Path) -> None:
    """A real same-model call between stream pulls should emit its own usage event."""
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=100, cache_read_tokens=80))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(),
        default_log_dir=tmp_path,
    )

    with capture_logs() as logs:
        stream = model.ainvoke_stream(
            messages=[Message(role="user", content="stream")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )
        first_chunk = await anext(stream)
        nested_response = await model.ainvoke(
            messages=[Message(role="user", content="nested")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )
        remaining_chunks = [chunk async for chunk in stream]

    assert first_chunk.content == "ok"
    assert nested_response.content == "ok"
    assert [chunk.content for chunk in remaining_chunks] == ["!"]
    assert [entry["event"] for entry in logs] == ["LLM usage", "LLM usage"]


@pytest.mark.asyncio
async def test_llm_response_usage_record_written_and_linked(tmp_path: Path) -> None:
    """A provider response with usage should append a response record joined by request_log_id."""
    model = _FakeModel(
        response_usage=MessageMetrics(input_tokens=5, output_tokens=7, cache_read_tokens=100, cache_write_tokens=20),
    )
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    with bind_llm_request_log_context(agent_id="assistant", session_id="session-1", correlation_id="corr-9"):
        await model.ainvoke(
            messages=[Message(role="user", content="hello")],
            assistant_message=Message(role="assistant"),
            tools=[],
        )

    request_entry, response_entry = _read_log_entries(tmp_path)
    assert request_entry["request_log_id"]
    assert "record" not in request_entry
    assert response_entry["record"] == "response"
    assert response_entry["request_log_id"] == request_entry["request_log_id"]
    assert response_entry["agent_id"] == "default"
    assert response_entry["model_id"] == "test-model"
    assert response_entry["correlation_id"] == "corr-9"
    assert response_entry["usage"] == {
        "input_tokens": 5,
        "output_tokens": 7,
        "cache_read_tokens": 100,
        "cache_write_tokens": 20,
    }


@pytest.mark.asyncio
async def test_llm_response_usage_record_uses_final_stream_chunk(tmp_path: Path) -> None:
    """Streaming should record the usage carried by the last usage-bearing chunk."""
    model = _FakeModel(response_usage=MessageMetrics(input_tokens=3, cache_read_tokens=42))
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    stream = model.ainvoke_stream(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )
    assert [chunk.content async for chunk in stream] == ["ok", "!"]

    request_entry, response_entry = _read_log_entries(tmp_path)
    assert response_entry["record"] == "response"
    assert response_entry["request_log_id"] == request_entry["request_log_id"]
    assert response_entry["usage"]["cache_read_tokens"] == 42
    assert response_entry["usage"]["output_tokens"] == 0


@pytest.mark.asyncio
async def test_llm_response_record_skipped_without_usage(tmp_path: Path) -> None:
    """Responses without usage metrics should not produce response records."""
    model = _FakeModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    await model.ainvoke(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )

    entries = _read_log_entries(tmp_path)
    assert len(entries) == 1
    assert "record" not in entries[0]


@pytest.mark.asyncio
async def test_llm_response_usage_recorded_when_stream_is_abandoned(tmp_path: Path) -> None:
    """Usage seen before an early aclose() must still produce a response record."""

    @dataclass
    class _EarlyUsageModel(_FakeModel):
        async def ainvoke_stream(self, *_args: object, **_kwargs: object) -> AsyncIterator[ModelResponse]:
            yield ModelResponse(content="ok", response_usage=MessageMetrics(input_tokens=3, cache_read_tokens=42))
            yield ModelResponse(content="never consumed")

    model = _EarlyUsageModel()
    install_llm_request_logging(
        model,
        agent_name="default",
        debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(tmp_path)),
        default_log_dir=tmp_path / "unused",
    )

    stream = model.ainvoke_stream(
        messages=[Message(role="user", content="hello")],
        assistant_message=Message(role="assistant"),
        tools=[],
    )
    first_chunk = await anext(stream)
    assert first_chunk.content == "ok"
    await stream.aclose()

    request_entry, response_entry = _read_log_entries(tmp_path)
    assert response_entry["record"] == "response"
    assert response_entry["request_log_id"] == request_entry["request_log_id"]
    assert response_entry["usage"]["cache_read_tokens"] == 42


@pytest.mark.asyncio
async def test_llm_response_record_reuses_the_request_records_file(tmp_path: Path) -> None:
    """The response record must land in the request record's daily file, not the current day's."""
    request_day_file = tmp_path / "llm-requests-2026-01-01.jsonl"
    await _write_llm_response_log(
        model=_FakeModel(),
        agent_name="default",
        request_log_ref=_RequestLogRef(request_log_id="req-1", log_path=request_day_file),
        usage=MessageMetrics(input_tokens=1, cache_read_tokens=5),
        request_context={},
    )

    entries = [json.loads(line) for line in request_day_file.read_text(encoding="utf-8").splitlines()]
    assert entries[0]["record"] == "response"
    assert entries[0]["request_log_id"] == "req-1"
