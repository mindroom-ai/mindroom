"""Tests for the MindRoom tool-calling proxy."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.proxy import (
    ProxyConfig,
    _CollectedToolCall,
    _parse_sse_events,
    _parse_sse_line,
    _StreamParseResult,
    create_proxy_app,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------


class TestParseSSELine:
    """Tests for _parse_sse_line()."""

    def test_valid_json(self) -> None:
        """Valid JSON data line is parsed."""
        line = 'data: {"choices": [{"delta": {"content": "hi"}}]}'
        result = _parse_sse_line(line)
        assert result is not None
        assert result["choices"][0]["delta"]["content"] == "hi"

    def test_done_marker(self) -> None:
        """[DONE] marker returns None."""
        assert _parse_sse_line("data: [DONE]") is None

    def test_empty_line(self) -> None:
        """Empty line returns None."""
        assert _parse_sse_line("") is None

    def test_non_data_line(self) -> None:
        """Non-data line returns None."""
        assert _parse_sse_line("event: message") is None

    def test_invalid_json(self) -> None:
        """Invalid JSON returns None."""
        assert _parse_sse_line("data: {not json}") is None


class TestParseSSEEvents:
    """Tests for _parse_sse_events()."""

    def test_content_chunks(self) -> None:
        """Content chunks are extracted from SSE stream."""
        sse = (
            'data: {"choices":[{"delta":{"role":"assistant"},"index":0}],"model":"general"}\n'
            'data: {"choices":[{"delta":{"content":"Hello"},"index":0}],"model":"general"}\n'
            'data: {"choices":[{"delta":{"content":" world"},"index":0}],"model":"general"}\n'
            'data: {"choices":[{"delta":{},"index":0,"finish_reason":"stop"}],"model":"general"}\n'
            "data: [DONE]\n"
        )
        result = _parse_sse_events(sse)
        assert result.content_chunks == ["Hello", " world"]
        assert result.finish_reason == "stop"
        assert result.model == "general"
        assert result.tool_calls == []

    def test_tool_calls_extracted(self) -> None:
        """Tool calls are extracted from delta.tool_calls."""
        sse = (
            'data: {"choices":[{"delta":{"role":"assistant"},"index":0}],"model":"test"}\n'
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_abc","function":{"name":"search","arguments":"{\\"q\\": \\"test\\"}"}}]},"index":0}],"model":"test"}\n'
            'data: {"choices":[{"delta":{},"index":0,"finish_reason":"tool_calls"}],"model":"test"}\n'
            "data: [DONE]\n"
        )
        result = _parse_sse_events(sse)
        assert result.finish_reason == "tool_calls"
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc.id == "call_abc"
        assert tc.name == "search"
        assert json.loads(tc.arguments) == {"q": "test"}

    def test_multiple_tool_calls(self) -> None:
        """Multiple tool calls at different indices are collected."""
        sse = (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"search","arguments":"{}"}}]},"index":0}],"model":"test"}\n'
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_2","function":{"name":"read_file","arguments":"{\\"path\\": \\"/tmp\\"}"}}]},"index":0}],"model":"test"}\n'
            'data: {"choices":[{"delta":{},"index":0,"finish_reason":"tool_calls"}],"model":"test"}\n'
        )
        result = _parse_sse_events(sse)
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].name == "search"
        assert result.tool_calls[1].name == "read_file"

    def test_empty_sse(self) -> None:
        """Empty SSE returns empty result."""
        result = _parse_sse_events("")
        assert result.content_chunks == []
        assert result.tool_calls == []
        assert result.finish_reason is None

    def test_non_streaming_message(self) -> None:
        """Non-streaming response with message.tool_calls."""
        sse = 'data: {"choices":[{"message":{"role":"assistant","content":null,"tool_calls":[{"id":"call_x","type":"function","function":{"name":"web_search","arguments":"{\\"q\\":\\"hi\\"}"}}]},"index":0,"finish_reason":"tool_calls"}],"model":"agent"}\n'
        result = _parse_sse_events(sse)
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].id == "call_x"
        assert result.tool_calls[0].name == "web_search"
        assert result.finish_reason == "tool_calls"

    def test_chunked_arguments(self) -> None:
        """Tool call arguments split across multiple chunks."""
        sse = (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_c","function":{"name":"search","arguments":"{\\"q\\":"}}]},"index":0}],"model":"test"}\n'
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":" \\"hello\\"}"}}]},"index":0}],"model":"test"}\n'
            'data: {"choices":[{"delta":{},"index":0,"finish_reason":"tool_calls"}],"model":"test"}\n'
        )
        result = _parse_sse_events(sse)
        assert len(result.tool_calls) == 1
        assert json.loads(result.tool_calls[0].arguments) == {"q": "hello"}


# ---------------------------------------------------------------------------
# Proxy app integration
# ---------------------------------------------------------------------------


class TestProxyApp:
    """Integration tests for the proxy FastAPI app."""

    @pytest.fixture
    def proxy_app(self) -> FastAPI:
        """Create a proxy app with a fake upstream."""
        config = ProxyConfig(upstream="http://fake-upstream:8765")
        return create_proxy_app(config)

    def test_models_endpoint_exists(self, proxy_app: FastAPI) -> None:
        """The /v1/models route is registered."""
        routes = [r.path for r in proxy_app.routes]
        assert "/v1/models" in routes

    def test_chat_endpoint_exists(self, proxy_app: FastAPI) -> None:
        """The /v1/chat/completions route is registered."""
        routes = [r.path for r in proxy_app.routes]
        assert "/v1/chat/completions" in routes


class TestCollectedToolCall:
    """Tests for _CollectedToolCall dataclass."""

    def test_creation(self) -> None:
        """Dataclass fields are set correctly."""
        tc = _CollectedToolCall(id="call_123", name="search", arguments='{"q": "test"}')
        assert tc.id == "call_123"
        assert tc.name == "search"
        assert tc.arguments == '{"q": "test"}'


class TestStreamParseResult:
    """Tests for _StreamParseResult dataclass."""

    def test_defaults(self) -> None:
        """Default values are empty lists and None."""
        result = _StreamParseResult()
        assert result.content_chunks == []
        assert result.tool_calls == []
        assert result.finish_reason is None
        assert result.model is None
