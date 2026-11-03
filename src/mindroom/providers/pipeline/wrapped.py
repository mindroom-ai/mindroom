"""The provider wrapper every pipeline stage uses, and the attempt runner retrying stages share.

A stage is a function ``stage(inner, request)`` that returns the request's event stream; ``WrappedProvider``
presents it as a ``ModelProvider`` so stages compose by nesting and callers never see the difference.

Usage invariant across retries: every attempt's ``UsageReported`` is forwarded, and the final
``TurnCompleted`` message's metrics add the usage of the attempts that failed before it. A partial report stands
in for its attempt until a final one replaces it, so before a retry a failed attempt's partial report that no final
one followed is forwarded again as final. The final reports of one request therefore always sum to its finished
turn's metrics, and a consumer settling a stream that never completed sums them and adds any partial report no
final one replaced.

Once failed attempts reported usage, one rule marks the usage of each later attempt as summing attempts: its reports
get ``sums_attempts`` and its finished turn ``provider_data[AGGREGATE_USAGE_KEY]``, so a request that completes
and one cut off before it finished both store the sum as describing no single request. A finished turn whose attempt
was reissued as another request, such as one without the media a provider rejected, gets
``provider_data[REWRITTEN_ATTEMPT_KEY]``: its usage describes that other request, not the one the caller sent.

A stage that rewrites every request it streams the same way, with no state of its own, names that rewrite as its
``projection``, so the wrapper sizes and identifies the request its inner provider is sent.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.llm.errors import ProviderError, incomplete_stream_error
from mindroom.llm.messages import AGGREGATE_USAGE_KEY, REWRITTEN_ATTEMPT_KEY, stable_serialize
from mindroom.llm.provider import (
    ReasoningDelta,
    ReportedUsage,
    StreamProgress,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.usage import add_usage
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mindroom.llm.provider import (
        Capabilities,
        ModelIdentity,
        ModelProvider,
        ModelRequest,
        NativeHistoryRoute,
        StreamEvent,
        TokenCounter,
    )
    from mindroom.llm.usage import MessageMetrics

__all__ = ["RequestProjection", "Retry", "WrappedProvider", "attempts"]

type _Stage = Callable[[ModelProvider, ModelRequest], AsyncIterator[StreamEvent]]


@dataclass(frozen=True, slots=True)
class RequestProjection:
    """How a stage rewrites every request before its inner provider is sent it, which its ``stream`` applies too.

    ``rewrite`` keeps the request's messages one for one. ``inputs`` returns what the rewrite reads besides the request,
    such as the media kinds a route learned its model rejects, so a request's identity changes when they do.
    """

    rewrite: Callable[[ModelProvider, ModelRequest], ModelRequest]
    inputs: Callable[[ModelProvider], object] | None = None


@dataclass(frozen=True, slots=True)
class WrappedProvider:
    """A provider whose ``stream`` runs ``stage`` around ``inner``, which is sent ``projection``'s rewrite."""

    inner: ModelProvider
    stage: _Stage
    projection: RequestProjection | None = None

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
    def token_counter(self) -> TokenCounter:
        """The wrapped provider's token counter."""
        return self.inner.token_counter

    @property
    def model_params(self) -> dict[str, Any]:
        """The wrapped provider's recorded settings."""
        return self.inner.model_params

    def request_tokens(self, request: ModelRequest, *, start: int = 0) -> int:
        """Size the request the wrapped provider is sent."""
        return self.inner.request_tokens(self._projected(request), start=start)

    def context_key(self, request: ModelRequest) -> str:
        """Identify the request the wrapped provider is sent, with what the projection reads besides it."""
        key = self.inner.context_key(self._projected(request))
        if self.projection is None or self.projection.inputs is None:
            return key
        inputs = stable_serialize(self.projection.inputs(self.inner))
        return hashlib.sha256(f"{key}\0{inputs}".encode("utf-8", "surrogatepass")).hexdigest()

    def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Run the stage around the wrapped provider's stream."""
        return self.stage(self.inner, request)

    def _projected(self, request: ModelRequest) -> ModelRequest:
        return request if self.projection is None else self.projection.rewrite(self.inner, request)


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
    sent = request
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
                    if isinstance(event, TurnCompleted) and request is not sent:
                        # The attempt was another request than the caller's, so its usage sizes none of the caller's.
                        message = event.message
                        message.provider_data = {**(message.provider_data or {}), REWRITTEN_ATTEMPT_KEY: True}
                    yield _with_carried_usage(event, carried)
            if finished:
                return
            raise incomplete_stream_error(provider=inner.identity.provider)
        except ProviderError as error:
            if committed or (reissue := retry(error)) is None:
                raise
        if (report := usage.settle()) is not None:
            yield _with_carried_usage(report, carried)
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
    """Add the failed attempts' usage to a reissued attempt's finished turn, marking its usage as summing attempts.

    The mark goes on the attempt's reports and its finished turn alike, once the failed attempts reported tokens.
    Counters sum, and the reported ``context`` stays the answering attempt's.
    """
    if carried is None:
        return event
    sums_attempts = carried.reported
    if isinstance(event, UsageReported):
        return replace(event, sums_attempts=True) if sums_attempts else event
    if not isinstance(event, TurnCompleted):
        return event
    message = event.message
    if sums_attempts:
        message.provider_data = {**(message.provider_data or {}), AGGREGATE_USAGE_KEY: True}
    message.metrics = carried + message.metrics
    return event
