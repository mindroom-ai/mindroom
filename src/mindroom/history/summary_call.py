"""One portable summary call and its bounded retry policy.

The summary request goes through the provider pipeline as one non-streaming call with every retry below this module,
the SDK's and the pipeline's, and the prompt cache turned off, and thinking off where the route can turn it off, bounded
by the compaction deadline. This module owns the call's lifecycle, cancellation, complete-output validation, and the
retry decision chunk execution uses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from mindroom.cancellation import request_task_cancel
from mindroom.history.types import COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS
from mindroom.llm.errors import (
    TRANSPORT_TIMEOUT,
    ContextWindowExceeded,
    ModelSafeguardRefusal,
    ProviderError,
    RateLimited,
)
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, RequestOverrides, complete
from mindroom.llm.usage import TokenCounts
from mindroom.logging_config import get_logger
from mindroom.providers.errors import is_context_window_error
from mindroom.timing import timed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.llm.provider import ModelProvider, ModelTurn
    from mindroom.llm.usage import MessageMetrics

logger = get_logger(__name__)

_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0
# A provider that times out on its own side says so only in the text of its HTTP error or stream error event, such as
# a gateway's "upstream request timed out": no status or code marks it, so provider errors are matched by wording.
_PROVIDER_TIMEOUT_FRAGMENT = "timed out"
# ``SummaryRetryPolicy`` alone retries a failed summary, a one-off summary never reuses its prefix, and thinking would
# spend the output budget the summary needs. The transport timeout stays the route's own, so a shorter authored
# provider timeout still applies; the summary call's wall deadline bounds every request.
_SUMMARY_OVERRIDES = RequestOverrides(caller_retries=True, prompt_cache=False, disable_thinking=True)


class _CompactionSummaryTimeoutError(TimeoutError):
    """The compaction-owned wall deadline expired; independent of provider wording."""


class CompactionSummaryOutputLimitError(RuntimeError):
    """Raised when the summary response reaches the configured output-token cap."""


class CompactionSummaryIncompleteError(RuntimeError):
    """Raised when a provider returns partial text without completing the summary."""


class _CompactionSummaryEmptyResultError(RuntimeError):
    """Raised when the summary model returns a success response with no text."""


class _CompactionSummaryUsageError(RuntimeError):
    """Usage persistence failed after a paid response; never retry the model for this."""


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
    """Return whether a compaction failure was a timeout: the deadline, a transport timeout, or a provider-side one."""
    if isinstance(error, TimeoutError):
        return True
    if not isinstance(error, ProviderError):
        return False
    return error.code == TRANSPORT_TIMEOUT or _PROVIDER_TIMEOUT_FRAGMENT in str(error).casefold()


def _is_size_error(error: Exception) -> bool:
    """Return whether a rate limit says the input alone was too large, or a final request timed out.

    A request above the account's tokens-per-minute limit fails as a rate limit worded "Request too large"; waiting
    never lets it through, but a smaller input does.
    """
    oversized = isinstance(error, RateLimited) and is_context_window_error(str(error), code=error.code)
    return oversized or (is_summary_timeout(error) and not _is_transient(error))


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
        return isinstance(error, _TYPED_SHRINKABLE_ERRORS) or _is_size_error(error)

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
        if isinstance(error, _CompactionSummaryUsageError) or attempt >= self.max_attempts:
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
    """Send the summary request, then persist the usage it reported, a refused response's included."""
    try:
        turn: ModelTurn | ModelSafeguardRefusal = await complete(model, request)
    except ModelSafeguardRefusal as refusal:
        turn = refusal
    finally:
        provider_done.set_result(None)
    if on_usage is not None and turn.usage.tokens != TokenCounts():
        try:
            await on_usage(turn.usage)
        except Exception as error:
            msg = "Could not persist compaction summary usage"
            raise _CompactionSummaryUsageError(msg) from error
    if isinstance(turn, ModelSafeguardRefusal):
        raise turn
    return turn


@timed("system_prompt_assembly.history_prepare.compaction.summary_model_request")
async def generate_compaction_summary(
    *,
    model: ModelProvider,
    summary_input: str,
    summary_prompt: str,
    timeout_seconds: float,
    on_usage: Callable[[MessageMetrics], Awaitable[None]] | None = None,
) -> str:
    """Bound the provider request, then persist returned usage before validating the summary text it returns."""
    provider_done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    request = ModelRequest(
        messages=build_summary_request_messages(summary_prompt=summary_prompt, summary_input=summary_input),
        stream=False,
        overrides=_SUMMARY_OVERRIDES,
    )
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


def _normalize_compaction_summary_text(raw_text: str) -> str:
    normalized = raw_text.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        first_newline = normalized.find("\n")
        if first_newline != -1:
            normalized = normalized[first_newline + 1 : -3].strip()
    return normalized
