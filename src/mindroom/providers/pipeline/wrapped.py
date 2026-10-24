"""The provider wrapper every pipeline stage uses, and the attempt runner retrying stages share.

A stage is a function ``stage(inner, request)`` that returns the request's event stream; ``WrappedProvider``
presents it as a ``ModelProvider`` so stages compose by nesting and callers never see the difference.

Usage invariant across retries: every attempt's ``UsageReported`` is forwarded, and the final
``TurnCompleted`` message's metrics add the usage of the attempts that failed before it, marked with
``provider_data["mindroom_aggregate_usage"]``. A partial report stands in for its attempt until a final one
replaces it, so before a retry a failed attempt's partial report that no final one followed is forwarded
again as final. The final reports of one request therefore always sum to its finished turn's metrics, and a
consumer settling a stream that never completed sums them and adds any partial report no final one replaced.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.llm.errors import ProviderError, incomplete_stream_error
from mindroom.llm.messages import AGGREGATE_USAGE_KEY
from mindroom.llm.provider import ReasoningDelta, ReportedUsage, StreamProgress, TextDelta, ToolCallReady, TurnCompleted
from mindroom.llm.usage import TokenCounts, add_usage
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

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

__all__ = ["Retry", "WrappedProvider", "attempts"]

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


@dataclass(frozen=True, slots=True)
class Retry:
    """How a retrying stage reissues a failed attempt: the request to send, after ``delay_seconds``."""

    request: ModelRequest
    delay_seconds: float = 0.0


async def attempts(
    inner: ModelProvider,
    request: ModelRequest,
    retry: Callable[[ProviderError], Retry | None],
    *,
    next_event: Callable[[AsyncIterator[StreamEvent]], Awaitable[StreamEvent]] = anext,
) -> AsyncIterator[StreamEvent]:
    """Stream ``request``, reissuing it as ``retry`` decides after a failure that came before the attempt committed.

    ``retry`` sees only the failures of attempts that committed nothing, and returns how to reissue the request, or
    None to let the failure propagate; ``next_event`` reads each event, so a stage can bound the wait for it. A failed
    attempt's partial usage is settled before the retry, and the finished turn carries the failed attempts' usage.
    An attempt whose stream ends without ``TurnCompleted`` ended before the provider finished the turn, on every
    route, and fails with the retryable ``incomplete_stream_error``.
    """
    carried: MessageMetrics | None = None
    while True:
        committed = False
        finished = False
        usage = ReportedUsage()
        try:
            events = inner.stream(request)
            async with closing_async_stream(events):
                while True:
                    try:
                        event = await next_event(events)
                    except StopAsyncIteration:
                        break
                    usage.observe(event)
                    committed = committed or _commits_attempt(event)
                    finished = finished or isinstance(event, TurnCompleted)
                    yield _with_carried_usage(event, carried)
            if finished:
                return
            raise incomplete_stream_error(provider=inner.identity.provider_name)
        except ProviderError as error:
            if committed or (reissue := retry(error)) is None:
                raise
        if (report := usage.settle()) is not None:
            yield report
        if reissue.delay_seconds:
            await asyncio.sleep(reissue.delay_seconds)
        carried = add_usage(carried, usage.settled)
        request = reissue.request


def _commits_attempt(event: StreamEvent) -> bool:
    """Return whether a retry after this event could repeat work: output the caller received, or a hosted tool's run.

    It decides for a failure the adapter raised and for a stage's own stall alike.
    """
    if isinstance(event, TextDelta | ReasoningDelta):
        return bool(event.text)
    if isinstance(event, StreamProgress):
        return event.commits
    return isinstance(event, ToolCallReady)


def _with_carried_usage(event: StreamEvent, carried: MessageMetrics | None) -> StreamEvent:
    """Add usage reported by failed attempts to a finished turn, marking the sum."""
    if carried is None or not isinstance(event, TurnCompleted):
        return event
    message = event.message
    if carried.tokens != TokenCounts():
        message.provider_data = {**(message.provider_data or {}), AGGREGATE_USAGE_KEY: True}
    message.metrics = message.metrics + carried
    return event
