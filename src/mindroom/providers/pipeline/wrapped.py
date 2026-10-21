"""The provider wrapper every pipeline stage uses, and the attempt bookkeeping retrying stages share.

A stage is an async generator function ``stage(inner, request)``; ``WrappedProvider`` presents it as a
``ModelProvider`` so stages compose by nesting and callers never see the difference.

Usage invariant across retries: every attempt's ``UsageReported`` is forwarded, and the final
``TurnCompleted`` message's metrics add the usage of the attempts that failed before it, marked with
``provider_data["mindroom_aggregate_usage"]``. The reported usages of one request therefore always sum to
its finished turn's metrics, and a consumer settling a stream that never completed sums the reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady, TurnCompleted
from mindroom.usage.storage import has_token_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from mindroom.llm.messages import Message
    from mindroom.llm.provider import Capabilities, ModelIdentity, ModelProvider, ModelRequest, StreamEvent
    from mindroom.llm.usage import MessageMetrics

__all__ = ["AGGREGATE_USAGE_KEY", "WrappedProvider", "add_usage", "is_visible_output", "with_carried_usage"]

AGGREGATE_USAGE_KEY = "mindroom_aggregate_usage"

type _Stage = Callable[[ModelProvider, ModelRequest], AsyncIterator[StreamEvent]]


@dataclass(frozen=True, slots=True)
class WrappedProvider:
    """A provider whose ``stream`` runs ``stage`` around ``inner``."""

    inner: ModelProvider
    stage: _Stage

    @property
    def identity(self) -> ModelIdentity:
        """The wrapped provider's identity."""
        return self.inner.identity

    @property
    def capabilities(self) -> Capabilities:
        """The wrapped provider's capabilities."""
        return self.inner.capabilities

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Run the stage around the wrapped provider's stream."""
        return self.stage(self.inner, request)

    def format_tool_results(self, messages: Sequence[Message], results: Sequence[Message]) -> list[Message]:
        """Delegate tool-result formatting to the wrapped provider."""
        return self.inner.format_tool_results(messages, results)


def is_visible_output(event: StreamEvent) -> bool:
    """Return whether retrying after this event would duplicate output the caller already received."""
    if isinstance(event, TextDelta | ReasoningDelta):
        return bool(event.text)
    return isinstance(event, ToolCallReady)


def add_usage(total: MessageMetrics | None, usage: MessageMetrics | None) -> MessageMetrics | None:
    """Return the sum of two optional usage reports."""
    if total is None or usage is None:
        return usage if total is None else total
    return total + usage


def with_carried_usage(event: StreamEvent, carried: MessageMetrics | None) -> StreamEvent:
    """Add usage reported by failed attempts to a finished turn, marking the sum."""
    if carried is None or not isinstance(event, TurnCompleted):
        return event
    message = event.message
    if has_token_usage(carried.to_dict()):
        message.provider_data = {**(message.provider_data or {}), AGGREGATE_USAGE_KEY: True}
    message.metrics = message.metrics + carried
    return event
