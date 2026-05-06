"""Tests for context-bound async stream helpers."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

import pytest

from mindroom.llm_request_logging import (
    bind_llm_request_log_context,
    current_llm_request_log_context,
    stream_with_llm_request_log_context,
)
from mindroom.tool_system.context_bound_streams import context_bound_async_stream
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    get_tool_execution_identity,
    stream_with_tool_execution_identity,
    tool_execution_identity,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator


_STREAM_CONTEXT: ContextVar[str | None] = ContextVar("test_stream_context", default=None)


@contextmanager
def _bind_stream_context(value: str | None) -> Iterator[None]:
    token = _STREAM_CONTEXT.set(value)
    try:
        yield
    finally:
        _STREAM_CONTEXT.reset(token)


class _ClosableStream:
    def __init__(self, values: list[str]) -> None:
        self._values = values
        self.observed_contexts: list[str | None] = []

    def __aiter__(self) -> _ClosableStream:
        return self

    async def __anext__(self) -> str:
        if not self._values:
            raise StopAsyncIteration
        self.observed_contexts.append(_STREAM_CONTEXT.get())
        return self._values.pop(0)

    async def aclose(self) -> None:
        self.observed_contexts.append(_STREAM_CONTEXT.get())


class _ClosableIdentityStream:
    def __init__(self, values: list[str]) -> None:
        self._values = values
        self.observed_identities: list[ToolExecutionIdentity | None] = []

    def __aiter__(self) -> _ClosableIdentityStream:
        return self

    async def __anext__(self) -> str:
        if not self._values:
            raise StopAsyncIteration
        self.observed_identities.append(get_tool_execution_identity())
        return self._values.pop(0)

    async def aclose(self) -> None:
        self.observed_identities.append(get_tool_execution_identity())


class _ClosableRequestLogStream:
    def __init__(self, values: list[str]) -> None:
        self._values = values
        self.observed_contexts: list[dict[str, object]] = []

    def __aiter__(self) -> _ClosableRequestLogStream:
        return self

    async def __anext__(self) -> str:
        if not self._values:
            raise StopAsyncIteration
        self.observed_contexts.append(current_llm_request_log_context())
        return self._values.pop(0)

    async def aclose(self) -> None:
        self.observed_contexts.append(current_llm_request_log_context())


def _identity(agent_name: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=f"@{agent_name}:example.com",
        room_id="!room:example.com",
        thread_id=None,
        resolved_thread_id=None,
        session_id=f"{agent_name}-session",
    )


@pytest.mark.asyncio
async def test_context_bound_async_stream_binds_factory_next_and_close_without_spanning_yields() -> None:
    """The wrapper should bind context only while touching the wrapped stream."""
    source = _ClosableStream(["first", "second"])
    observed_factory_contexts: list[str | None] = []
    observed_yield_contexts: list[str | None] = []

    def factory() -> AsyncIterator[str]:
        observed_factory_contexts.append(_STREAM_CONTEXT.get())
        return source

    with _bind_stream_context("outer"):
        stream = context_bound_async_stream(
            context_factory=lambda: _bind_stream_context("inner"),
            stream_factory=factory,
        )
        observed_yield_contexts.append(_STREAM_CONTEXT.get())
        assert await anext(stream) == "first"
        observed_yield_contexts.append(_STREAM_CONTEXT.get())
        await stream.aclose()
        observed_yield_contexts.append(_STREAM_CONTEXT.get())

    assert observed_factory_contexts == ["inner"]
    assert source.observed_contexts == ["inner", "inner"]
    assert observed_yield_contexts == ["outer", "outer", "outer"]


@pytest.mark.asyncio
async def test_tool_execution_identity_stream_binds_factory_next_and_close_without_spanning_yields() -> None:
    """Tool execution streams should not leak their identity into caller yield points."""
    inner_identity = _identity("inner")
    outer_identity = _identity("outer")
    source = _ClosableIdentityStream(["first", "second"])
    observed_factory_identities: list[ToolExecutionIdentity | None] = []
    observed_yield_identities: list[ToolExecutionIdentity | None] = []

    def factory() -> AsyncIterator[str]:
        observed_factory_identities.append(get_tool_execution_identity())
        return source

    with tool_execution_identity(outer_identity):
        stream = stream_with_tool_execution_identity(inner_identity, stream_factory=factory)
        observed_yield_identities.append(get_tool_execution_identity())
        assert await anext(stream) == "first"
        observed_yield_identities.append(get_tool_execution_identity())
        await stream.aclose()
        observed_yield_identities.append(get_tool_execution_identity())

    assert observed_factory_identities == [inner_identity]
    assert source.observed_identities == [inner_identity, inner_identity]
    assert observed_yield_identities == [outer_identity, outer_identity, outer_identity]


@pytest.mark.asyncio
async def test_llm_request_log_stream_binds_next_and_close_without_spanning_yields() -> None:
    """Request-log streams should bind request context only while touching the stream."""
    source = _ClosableRequestLogStream(["first", "second"])
    observed_yield_contexts: list[dict[str, object]] = []

    with bind_llm_request_log_context(correlation_id="outer"):
        stream = stream_with_llm_request_log_context(
            source,
            request_context={"correlation_id": "inner"},
        )
        observed_yield_contexts.append(current_llm_request_log_context())
        assert await anext(stream) == "first"
        observed_yield_contexts.append(current_llm_request_log_context())
        await stream.aclose()
        observed_yield_contexts.append(current_llm_request_log_context())

    assert source.observed_contexts == [{"correlation_id": "inner"}, {"correlation_id": "inner"}]
    assert observed_yield_contexts == [
        {"correlation_id": "outer"},
        {"correlation_id": "outer"},
        {"correlation_id": "outer"},
    ]
