"""One portable summary call and its bounded retry policy.

Provider request normalization and legacy error interpretation have explicit
compatibility owners. This module owns only call lifecycle, cancellation,
complete-output validation, and the retry decision used by chunk execution.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Literal

from agno.exceptions import ContextWindowExceededError
from agno.models.message import Message
from agno.session.summary import SessionSummary

from mindroom.cancellation import request_task_cancel
from mindroom.history.summary_provider_compat import (
    configure_summary_model,
    effective_summary_timeout_seconds,
    response_output_tokens,
    summary_completion_status,
    summary_output_token_limit,
)
from mindroom.history.types import COMPACTION_SUMMARY_RETRY_FLOOR_TOKENS
from mindroom.logging_config import get_logger
from mindroom.provider_error_compat import is_legacy_summary_size_error, is_transient_summary_error
from mindroom.timing import timed

if TYPE_CHECKING:
    from agno.models.base import Model
    from agno.models.response import ModelResponse

logger = get_logger(__name__)

_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0


class _CompactionSummaryTimeoutError(TimeoutError):
    """The compaction-owned wall deadline expired; independent of provider wording."""


class CompactionSummaryOutputLimitError(RuntimeError):
    """Raised when the summary response reaches the configured output-token cap."""


class CompactionSummaryIncompleteError(RuntimeError):
    """Raised when a provider returns partial text without completing the summary."""


class _CompactionSummaryEmptyResultError(RuntimeError):
    """Raised when the summary model returns a success response with no text."""


_TYPED_SHRINKABLE_ERRORS = (
    _CompactionSummaryEmptyResultError,
    TimeoutError,
    ContextWindowExceededError,
    CompactionSummaryOutputLimitError,
)


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
    and to the caller's smallest progress-preserving rebuild, while selected typed
    transient failures wait ``same_input_retry_delay_seconds`` and retry the
    same configured budget.
    Once ``max_attempts`` is reached or no retry applies, the error propagates.
    """

    max_attempts: int = 2
    shrink_divisor: int = 2
    same_input_retry_delay_seconds: float = 1.0

    def should_shrink(self, error: Exception) -> bool:
        """Return whether rebuilding a smaller summary input may resolve the failure."""
        if isinstance(error, _TYPED_SHRINKABLE_ERRORS):
            return True
        return is_legacy_summary_size_error(error)

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
        if is_transient_summary_error(error):
            return SummaryRetryDecision(budget=budget, kind="same-budget-transient")
        return None


DEFAULT_SUMMARY_RETRY_POLICY = SummaryRetryPolicy()


def build_summary_request_messages(*, summary_prompt: str, summary_input: str) -> list[Message]:
    """Keep summary instructions separate from the serialized conversation."""
    return [
        Message(role="system", content=summary_prompt),
        Message(role="user", content=summary_input),
    ]


def _consume_detached_compaction_request_result(
    response_task: asyncio.Task[ModelResponse],
    *,
    log_message: str,
) -> None:
    """Consume a detached request result so late failures do not surface unhandled."""
    try:
        response_task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning(log_message, exc_info=True)


def _warn_if_detached_compaction_request_still_running(
    response_task: asyncio.Task[ModelResponse],
    *,
    reason: str,
) -> None:
    """Log when a detached provider request ignored cancellation past the grace window."""
    if response_task.done():
        return
    logger.warning(
        "Compaction request still running after cancellation grace period",
        reason=reason,
        timeout_seconds=_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS,
    )


def _detach_cancelled_compaction_request(
    response_task: asyncio.Task[ModelResponse],
    *,
    reason: str,
) -> None:
    """Detach one cancelled provider request without blocking the caller or leaking cleanup tasks."""
    response_task.add_done_callback(
        partial(
            _consume_detached_compaction_request_result,
            log_message="Detached compaction request raised after caller moved on",
        ),
    )
    asyncio.get_running_loop().call_later(
        _COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS,
        partial(
            _warn_if_detached_compaction_request_still_running,
            response_task,
            reason=reason,
        ),
    )


@timed("system_prompt_assembly.history_prepare.compaction.summary_model_request")
async def generate_compaction_summary(
    *,
    model: Model,
    summary_input: str,
    summary_prompt: str,
    timeout_seconds: float,
) -> SessionSummary:
    """Issue one compaction summary call with tuned provider config and one timeout."""
    timeout_seconds = effective_summary_timeout_seconds(model, timeout_seconds=timeout_seconds)
    configured_model = configure_summary_model(model, timeout_seconds=timeout_seconds)
    summary_output_limit = summary_output_token_limit(configured_model)

    async def _request_summary() -> ModelResponse:
        return await model.aresponse(
            messages=build_summary_request_messages(summary_prompt=summary_prompt, summary_input=summary_input),
        )

    response_task = asyncio.create_task(
        _request_summary(),
        name="compaction_summary_request",
    )
    try:
        done, _pending = await asyncio.wait(
            {response_task},
            timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        request_task_cancel(response_task)
        _detach_cancelled_compaction_request(
            response_task,
            reason="outer_cancellation",
        )
        raise

    if response_task not in done:
        request_task_cancel(response_task)
        _detach_cancelled_compaction_request(
            response_task,
            reason="timeout",
        )
        msg = f"compaction summary timed out after {timeout_seconds}s"
        raise _CompactionSummaryTimeoutError(msg)

    response = response_task.result()
    raw_text = response.content if isinstance(response.content, str) else ""
    normalized_text = _normalize_compaction_summary_text(raw_text)
    if not normalized_text:
        msg = (
            "summary generation returned no result "
            f"(output_tokens={response_output_tokens(response)}, "
            f"has_reasoning={bool(response.reasoning_content or response.redacted_reasoning_content)})"
        )
        raise _CompactionSummaryEmptyResultError(msg)
    completion = summary_completion_status(response, output_token_limit=summary_output_limit)
    if completion == "output_limit":
        msg = "compaction summary hit configured output token limit; refusing to persist incomplete summary"
        raise CompactionSummaryOutputLimitError(msg)
    if completion == "incomplete":
        msg = "provider returned an incomplete compaction summary; refusing to persist partial text"
        raise CompactionSummaryIncompleteError(msg)
    return SessionSummary(summary=normalized_text, updated_at=datetime.now(UTC))


def _normalize_compaction_summary_text(raw_text: str) -> str:
    normalized = raw_text.strip()
    if not normalized:
        return ""
    if normalized.startswith("```") and normalized.endswith("```"):
        first_newline = normalized.find("\n")
        if first_newline != -1:
            normalized = normalized[first_newline + 1 : -3].strip()
    return normalized
