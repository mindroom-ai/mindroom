"""One portable summary call and its bounded retry policy.

The summary request goes through the provider pipeline as one call, with every retry below this module and the prompt
cache turned off, and on the Claude routes without the model's authored thinking setting unless the model runs at its
``off`` reasoning level, bounded by the compaction
deadline. This module owns the call's lifecycle, cancellation, complete-output validation,
and the retry decision chunk execution uses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from mindroom.cancellation import request_task_cancel
from mindroom.history.types import COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS
from mindroom.llm.errors import (
    TRANSPORT_TIMEOUT,
    ContextWindowExceeded,
    ModelSafeguardRefusal,
    ProviderError,
    incomplete_stream_error,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ModelTurn, ReportedUsage, RequestOverrides, TurnCompleted
from mindroom.logging_config import get_logger
from mindroom.timing import timed
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.llm.provider import ModelProvider
    from mindroom.llm.usage import MessageMetrics

logger = get_logger(__name__)

_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0
# ``SummaryRetryPolicy`` alone retries a failed summary, and a one-off summary never reuses its prefix. Leaving out the
# authored thinking setting keeps a model that thinks only when asked, such as Claude 4.x, from spending the
# output budget the summary needs; a model that thinks by default still thinks. The transport timeout stays the route's
# own, so a shorter authored provider timeout still applies; the summary call's wall deadline bounds every request.
_SUMMARY_OVERRIDES = RequestOverrides(caller_retries=True, prompt_cache=False, omit_thinking_setting=True)


class _CompactionSummaryTimeoutError(TimeoutError):
    """The compaction-owned wall deadline expired; independent of provider wording."""


class CompactionSummaryOutputLimitError(RuntimeError):
    """Raised when the summary response reaches the configured output-token cap."""


class CompactionSummaryIncompleteError(RuntimeError):
    """Raised when a provider returns partial text without completing the summary."""


class CompactionForkToolCallError(CompactionSummaryIncompleteError):
    """Raised when a forked compaction call emits tool calls instead of summary text."""


class _CompactionSummaryEmptyResultError(RuntimeError):
    """Raised when the summary model returns a success response with no text."""


_TYPED_SHRINKABLE_ERRORS = (
    _CompactionSummaryEmptyResultError,
    TimeoutError,
    ContextWindowExceeded,
    CompactionSummaryOutputLimitError,
)


def _is_transient(error: Exception) -> bool:
    """Return whether the provider says the same request may succeed later, such as a rate limit or an outage."""
    return isinstance(error, ProviderError) and error.retryable


def is_summary_timeout(error: BaseException) -> bool:
    """Return whether a compaction failure was a timeout: the deadline, or a transport timeout the adapter typed."""
    return isinstance(error, TimeoutError) or (isinstance(error, ProviderError) and error.code == TRANSPORT_TIMEOUT)


@dataclass(frozen=True)
class SummaryRetryDecision:
    """One policy-owned retry action for the compaction summary caller."""

    budget: int
    kind: Literal["shrink", "same-budget-transient"]


@dataclass(frozen=True)
class SummaryRetryPolicy:
    """Explicit retry policy for failed compaction summary calls.

    Each shrinkable failure divides the actual serialized input size by
    ``shrink_divisor``, clamped to the shared compaction-summary retry floor
    and to the caller's smallest progress-preserving rebuild, while transient
    provider failures wait ``same_input_retry_delay_seconds`` and retry the
    same configured budget.
    Once ``max_attempts`` is reached or no retry applies, the error propagates.
    """

    max_attempts: int = 2
    shrink_divisor: int = 2
    same_input_retry_delay_seconds: float = 1.0

    def should_shrink(self, error: Exception) -> bool:
        """Return whether rebuilding a smaller summary input may resolve the failure."""
        return isinstance(error, _TYPED_SHRINKABLE_ERRORS)

    def retry_budget(
        self,
        *,
        attempt: int,
        budget: int,
        input_tokens: int,
        minimum_progress_input_tokens: int,
        error: Exception,
    ) -> SummaryRetryDecision | None:
        """Return the next retry action, or None when retries end.

        The decision kind is authoritative so callers cannot independently
        reclassify the error and apply shrink-only safeguards to a same-budget
        transient retry. ``minimum_progress_input_tokens`` is the smallest budget
        at which the caller can rebuild without dropping the prior summary or
        every run; shrink targets clamp there so a granted shrink is issued only
        when it rebuilds to a strictly smaller request with summarizable content.
        """
        if attempt >= self.max_attempts:
            return None
        if self.should_shrink(error):
            smaller_budget = min(
                budget,
                max(
                    COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS,
                    minimum_progress_input_tokens,
                    input_tokens // self.shrink_divisor,
                ),
            )
            if smaller_budget < input_tokens:
                return SummaryRetryDecision(budget=smaller_budget, kind="shrink")
        if _is_transient(error):
            return SummaryRetryDecision(budget=budget, kind="same-budget-transient")
        return None


DEFAULT_SUMMARY_RETRY_POLICY = SummaryRetryPolicy()


def build_summary_request_messages(*, summary_prompt: str, summary_input: str) -> list[Message]:
    """Keep summary instructions separate from the serialized conversation."""
    return [
        Message(role="system", content=summary_prompt),
        Message(role="user", content=summary_input),
    ]


def _consume_detached_compaction_request_result(response_task: asyncio.Task[ModelTurn]) -> None:
    """Consume a detached request result so late failures do not surface unhandled."""
    try:
        response_task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("Detached compaction request raised after caller moved on", exc_info=True)


def _cancel_and_detach(response_task: asyncio.Task[ModelTurn], *, reason: str) -> None:
    """Cancel one provider request without waiting for it, logging a late failure or a request that ignores the cancel."""
    request_task_cancel(response_task)
    response_task.add_done_callback(_consume_detached_compaction_request_result)

    def warn_if_still_running() -> None:
        if not response_task.done():
            logger.warning(
                "Compaction request still running after cancellation grace period",
                reason=reason,
                timeout_seconds=_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS,
            )

    asyncio.get_running_loop().call_later(_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS, warn_if_still_running)


async def _request_summary(
    model: ModelProvider,
    request: ModelRequest,
    provider_done: asyncio.Future[None],
    on_usage: Callable[[MessageMetrics], Awaitable[None]] | None,
) -> ModelTurn:
    """Send the summary request, then persist the usage it reported, failed and refused responses included."""
    reported = ReportedUsage()
    completed: TurnCompleted | None = None
    failure: Exception | None = None
    try:
        events = model.stream(request)
        async with closing_async_stream(events):
            async for event in events:
                reported.observe(event)
                if isinstance(event, TurnCompleted):
                    completed = event
    except Exception as error:
        failure = error
    finally:
        provider_done.set_result(None)
    if failure is None and completed is None:
        failure = incomplete_stream_error(provider=model.identity.provider)

    usage = (
        completed.message.metrics
        if completed is not None
        else reported.received
        if reported.received is not None and reported.received.reported
        else failure.usage
        if isinstance(failure, ModelSafeguardRefusal)
        else None
    )
    if on_usage is not None and usage is not None and usage.reported:
        try:
            await on_usage(usage)
        except Exception as error:
            logger.warning(
                "compaction_summary_usage_save_failed",
                error_type=type(error).__name__,
                exc_info=True,
            )
    if failure is not None:
        raise failure
    assert completed is not None
    return ModelTurn(message=completed.message, stop_reason=completed.stop_reason)


@timed("system_prompt_assembly.history_prepare.compaction.summary_model_request")
async def request_compaction_summary(
    *,
    model: ModelProvider,
    request: ModelRequest,
    timeout_seconds: float,
    on_usage: Callable[[MessageMetrics], Awaitable[None]] | None,
    reject_tool_calls: bool = False,
) -> str:
    """Bound one compaction summary request, persist returned usage, and validate the returned summary text."""
    provider_done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    response_task = asyncio.create_task(
        _request_summary(model, request, provider_done, on_usage),
        name="compaction_summary_request",
    )
    try:
        done, _pending = await asyncio.wait({provider_done}, timeout=timeout_seconds)
        if provider_done not in done:
            _cancel_and_detach(response_task, reason="timeout")
            msg = f"compaction summary timed out after {timeout_seconds}s"
            raise _CompactionSummaryTimeoutError(msg)
        turn = await asyncio.shield(response_task)
    except asyncio.CancelledError:
        _cancel_and_detach(response_task, reason="outer_cancellation")
        raise

    if reject_tool_calls and (turn.tool_calls or turn.stop_reason == "tool_use"):
        msg = "compaction fork attempted a tool call; refusing to persist summary"
        raise CompactionForkToolCallError(msg)
    normalized_text = _normalize_compaction_summary_text(turn.content)
    if not normalized_text:
        message = turn.message
        msg = (
            "summary generation returned no result "
            f"(output_tokens={turn.usage.tokens.output_tokens}, "
            f"has_reasoning={bool(message.reasoning_content or message.redacted_reasoning_content)})"
        )
        raise _CompactionSummaryEmptyResultError(msg)
    if turn.stop_reason == "output_limit":
        msg = "compaction summary hit configured output token limit; refusing to persist incomplete summary"
        raise CompactionSummaryOutputLimitError(msg)
    # A route that reports no stop reason counts as complete.
    if turn.stop_reason not in {None, "end"}:
        msg = "provider returned an incomplete compaction summary; refusing to persist partial text"
        raise CompactionSummaryIncompleteError(msg)
    return normalized_text


async def generate_compaction_summary(
    *,
    model: ModelProvider,
    summary_input: str,
    summary_prompt: str,
    timeout_seconds: float,
    on_usage: Callable[[MessageMetrics], Awaitable[None]] | None,
    keep_thinking_setting: bool = False,
) -> str:
    """Bound the provider request, then persist returned usage before validating the summary text it returns.

    ``keep_thinking_setting`` sends the model's thinking setting, which the request omits otherwise.
    """
    request = ModelRequest(
        messages=build_summary_request_messages(summary_prompt=summary_prompt, summary_input=summary_input),
        overrides=replace(_SUMMARY_OVERRIDES, omit_thinking_setting=not keep_thinking_setting),
    )
    return await request_compaction_summary(
        model=model,
        request=request,
        timeout_seconds=timeout_seconds,
        on_usage=on_usage,
    )


def _normalize_compaction_summary_text(raw_text: str) -> str:
    normalized = raw_text.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        first_newline = normalized.find("\n")
        if first_newline != -1:
            normalized = normalized[first_newline + 1 : -3].strip()
    return normalized
