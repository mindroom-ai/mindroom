"""Inspect model providers and their streams in tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.llm.provider import TurnCompleted
from mindroom.providers.pipeline.wrapped import WrappedProvider

if TYPE_CHECKING:
    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent


def bare_adapter(provider: ModelProvider) -> ModelProvider:
    """Return the adapter inside the pipeline stages the registry put around it.

    Production reads everything it needs through the ``ModelProvider`` contract; only tests that assert a route's
    settings, wire, or adapter class unwrap the pipeline.
    """
    while isinstance(provider, WrappedProvider):
        provider = provider.inner
    return provider


async def drain(provider: ModelProvider, request: ModelRequest) -> list[StreamEvent]:
    """Collect every event from a provider's stream."""
    return [event async for event in provider.stream(request)]


async def drain_into(events: list[StreamEvent], provider: ModelProvider, request: ModelRequest) -> None:
    """Append events as they arrive, keeping those a failing stream yielded before the failure."""
    async for event in provider.stream(request):
        events.append(event)  # noqa: PERF401


def completed(events: list[StreamEvent]) -> TurnCompleted:
    """Return the completion, asserting it was the last event."""
    turn = events[-1]
    assert isinstance(turn, TurnCompleted)
    return turn
