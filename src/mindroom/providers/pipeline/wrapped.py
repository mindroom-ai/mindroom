"""The provider wrapper every pipeline stage uses, and the attempt bookkeeping retrying stages share.

A stage is an async generator function ``stage(inner, request)``; ``WrappedProvider`` presents it as a
``ModelProvider`` so stages compose by nesting and callers never see the difference.

Usage invariant across retries: every attempt's ``UsageReported`` is forwarded, and the final
``TurnCompleted`` message's metrics add the usage of the attempts that failed before it, marked with
``provider_data["mindroom_aggregate_usage"]``. A partial report stands in for its attempt until a final one
replaces it, so before a retry a failed attempt's partial report that no final one followed is forwarded
again as final. The final reports of one request therefore always sum to its finished turn's metrics, and a
consumer settling a stream that never completed sums them and adds any partial report no final one replaced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.provider import ReasoningDelta, TextDelta, ToolCallReady, TurnCompleted, UsageReported
from mindroom.usage.storage import has_token_usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from mindroom.llm.provider import (
        Capabilities,
        ModelIdentity,
        ModelProvider,
        ModelRequest,
        NativeHistoryRoute,
        PortableReplayEstimator,
        StreamEvent,
    )
    from mindroom.llm.usage import MessageMetrics

__all__ = [
    "AGGREGATE_USAGE_KEY",
    "AttemptUsage",
    "WrappedProvider",
    "add_usage",
    "is_visible_output",
    "with_carried_usage",
]

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

    @property
    def native_history_route(self) -> NativeHistoryRoute | None:
        """The wrapped provider's native-history route."""
        return self.inner.native_history_route

    @property
    def replay_estimator(self) -> PortableReplayEstimator | None:
        """The wrapped provider's replay estimator."""
        return self.inner.replay_estimator

    @property
    def model_params(self) -> dict[str, Any]:
        """The wrapped provider's recorded settings."""
        return self.inner.model_params

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Run the stage around the wrapped provider's stream."""
        return self.stage(self.inner, request)


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


@dataclass(slots=True)
class AttemptUsage:
    """The usage one attempt reported: the sum of its final reports, and a later partial report none replaced yet."""

    settled: MessageMetrics | None = None
    pending: MessageMetrics | None = None

    def observe(self, event: StreamEvent) -> None:
        """Track one event the attempt forwarded."""
        if not isinstance(event, UsageReported):
            return
        if event.final:
            self.settled = add_usage(self.settled, event.usage)
            self.pending = None
        else:
            self.pending = event.usage

    def settle(self) -> UsageReported | None:
        """Count a failed attempt's pending partial report as settled, returning it as final to forward before a retry.

        The retry's own reports would otherwise replace it, and the caller would lose usage the provider billed.
        """
        if self.pending is None:
            return None
        report = UsageReported(self.pending)
        self.settled = add_usage(self.settled, self.pending)
        self.pending = None
        return report


def with_carried_usage(event: StreamEvent, carried: MessageMetrics | None) -> StreamEvent:
    """Add usage reported by failed attempts to a finished turn, marking the sum."""
    if carried is None or not isinstance(event, TurnCompleted):
        return event
    message = event.message
    if has_token_usage(carried.to_dict()):
        message.provider_data = {**(message.provider_data or {}), AGGREGATE_USAGE_KEY: True}
    message.metrics = message.metrics + carried
    return event
