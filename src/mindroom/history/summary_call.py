"""One compaction summary call: model tuning, request build, timeout, and retry policy.

This module is the only path for issuing a compaction summary model call.
It enforces the call-side half of the compaction invariants
(see ``tests/test_compaction_invariants.py``):

3. Summary calls get exactly one model configuration path.
   ``configure_summary_model`` applies all compaction-specific provider tuning in
   one place: prompt-cache writes off, Claude thinking cleared (a thinking budget
   at or above max_tokens is a 400 from Anthropic), SDK retries disabled, and
   one SDK timeout coordinated with the outer chunk budget
   (``MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS``) instead of two uncoordinated
   constants in two modules. Claude summary output uses the loaded model's own
   max_tokens as the truncation guard. Unknown providers pass through untouched
   and rely on the outer chunk timeout alone.

4. Retry on provider failure is deterministic.
   ``SummaryRetryPolicy`` decides which error classes warrant a smaller retry
   (timeouts, typed context-window errors, safeguard refusals, and named legacy
   context-length fragments), which warrant one same-input retry (typed transient
   provider errors), the shrink schedule (halving), and the give-up floor — no
   inline string matching at call sites.
   Empty-text success responses also retry with less input because some providers
   surface rejected or oversized requests as an empty successful response.

5. Output-capped summaries use an explicit retry signal.
   ``generate_compaction_summary`` refuses to return a likely truncated summary,
   and the retry wrapper can shrink input through ``SummaryRetryPolicy`` without
   depending on owned error-message text.

``build_summary_request_messages`` is the single replaceable request builder; a
future cache-friendly builder that reuses the active provider prefix (PR #861)
plugs in behind it without another cross-cutting diff.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING

from agno.exceptions import ContextWindowExceededError, ModelProviderError
from agno.models.message import Message
from agno.session.summary import SessionSummary

from mindroom.cancellation import request_task_cancel
from mindroom.claude_prompt_cache import as_anthropic_claude
from mindroom.constants import MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
from mindroom.error_handling import TRANSIENT_PROVIDER_STATUS_CODES, ModelSafeguardRefusalError
from mindroom.logging_config import get_logger
from mindroom.timing import timed

if TYPE_CHECKING:
    from agno.models.base import Model
    from agno.models.response import ModelResponse

logger = get_logger(__name__)

_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0

_RETRYABLE_PROVIDER_ERROR_FRAGMENTS = (
    "timed out",
    "context length",
    "context_length_exceeded",
    "too many tokens",
    "max tokens",
    "too large",
    "too long",
    "input size",
    "input too large",
    "maximum length",
    "max length",
    "request too large",
    "reduce the length",
)


class CompactionSummaryOutputLimitError(RuntimeError):
    """Raised when the summary response reaches the configured output-token cap."""


class _CompactionSummaryEmptyResultError(RuntimeError):
    """Raised when the summary model returns a success response with no text."""


@dataclass(frozen=True)
class SummaryRetryPolicy:
    """Explicit retry policy for failed compaction summary calls.

    The schedule is deterministic: each shrinkable failure divides the input
    budget by ``shrink_divisor`` (clamped to ``floor_tokens``), while transient
    provider errors wait ``same_input_retry_delay_seconds`` and keep the same
    input. Once the budget can no longer shrink, or ``max_attempts`` is reached,
    the error propagates.
    """

    max_attempts: int = 2
    shrink_divisor: int = 2
    floor_tokens: int = 1_000
    same_input_retry_delay_seconds: float = 1.0

    def should_shrink(self, error: Exception) -> bool:
        """Return whether a smaller summary input may resolve this provider failure."""
        if isinstance(
            error,
            TimeoutError | ContextWindowExceededError | ModelSafeguardRefusalError | CompactionSummaryOutputLimitError,
        ):
            return True
        message = str(error).lower()
        return any(fragment in message for fragment in _RETRYABLE_PROVIDER_ERROR_FRAGMENTS)

    def should_retry_same_input(self, error: Exception) -> bool:
        """Return whether a transient provider failure warrants one unchanged retry."""
        return isinstance(error, ModelProviderError) and error.status_code in TRANSIENT_PROVIDER_STATUS_CODES

    def retry_budget(self, *, attempt: int, budget: int, error: Exception) -> int | None:
        """Return the next input budget, preserving it for same-input retries."""
        if attempt >= self.max_attempts:
            return None
        typed_shrinkable = isinstance(
            error,
            (
                _CompactionSummaryEmptyResultError
                | TimeoutError
                | ContextWindowExceededError
                | ModelSafeguardRefusalError
                | CompactionSummaryOutputLimitError
            ),
        )
        if not typed_shrinkable and self.should_retry_same_input(error):
            return budget
        if typed_shrinkable or self.should_shrink(error):
            smaller_budget = max(self.floor_tokens, budget // self.shrink_divisor)
            if smaller_budget >= budget:
                return None
            return smaller_budget
        return None


DEFAULT_SUMMARY_RETRY_POLICY = SummaryRetryPolicy()


def configure_summary_model(model: Model, *, timeout_seconds: float | None = None) -> Model:
    """Apply all compaction-specific provider tuning to one loaded model (invariant 3).

    ``isinstance(model, Claude)`` covers the anthropic, vertexai_claude, and
    bedrock_claude providers because both forks subclass the Anthropic model.
    Mutating the instance is safe: ``get_model_instance`` builds a fresh model per
    call and compaction loads its own instance per run.
    """
    claude_model = as_anthropic_claude(model)
    if claude_model is None:
        logger.debug(
            "Compaction summary model tuning skipped",
            model_type=type(model).__name__,
            reason="provider_specific_tuning_only_defined_for_claude",
        )
        return model
    resolved_timeout = MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    claude_model.cache_system_prompt = False
    claude_model.extended_cache_time = False
    claude_model.thinking = None
    claude_model.timeout = min(claude_model.timeout, resolved_timeout) if claude_model.timeout else resolved_timeout
    client_params = dict(claude_model.client_params or {})
    client_params["max_retries"] = 0
    claude_model.client_params = client_params
    return model


def build_summary_request_messages(*, summary_prompt: str, summary_input: str) -> list[Message]:
    """Build the model request for one summary call (single replaceable seam for #861)."""
    return [
        Message(role="system", content=summary_prompt),
        Message(role="user", content=summary_input),
    ]


class _CompactionProviderTimeoutError(Exception):
    """Internal wrapper so provider TimeoutError does not look like our wait_for timeout."""

    def __init__(self, original: TimeoutError) -> None:
        super().__init__(str(original))
        self.original = original


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
    timeout_seconds: float | None = None,
) -> SessionSummary:
    """Issue one compaction summary call with tuned provider config and one timeout."""
    resolved_timeout = MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    configured_model = configure_summary_model(model, timeout_seconds=resolved_timeout)
    summary_output_limit = _summary_output_token_limit(configured_model)

    async def _request_summary() -> ModelResponse:
        try:
            return await model.aresponse(
                messages=build_summary_request_messages(
                    summary_prompt=summary_prompt,
                    summary_input=summary_input,
                ),
            )
        except TimeoutError as exc:
            raise _CompactionProviderTimeoutError(exc) from exc

    response_task = asyncio.create_task(
        _request_summary(),
        name="compaction_summary_request",
    )
    try:
        done, _pending = await asyncio.wait(
            {response_task},
            timeout=resolved_timeout,
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
        msg = f"compaction summary timed out after {resolved_timeout}s"
        raise RuntimeError(msg)

    try:
        response = response_task.result()
    except _CompactionProviderTimeoutError as exc:
        raise exc.original from exc
    raw_text = response.content if isinstance(response.content, str) else ""
    normalized_text = _normalize_compaction_summary_text(raw_text)
    if not normalized_text:
        msg = (
            "summary generation returned no result "
            f"(output_tokens={_response_output_tokens(response)}, "
            f"has_reasoning={bool(response.reasoning_content or response.redacted_reasoning_content)})"
        )
        raise _CompactionSummaryEmptyResultError(msg)
    if _summary_response_likely_truncated(response, output_token_limit=summary_output_limit):
        msg = "compaction summary hit configured output token limit; refusing to persist incomplete summary"
        raise CompactionSummaryOutputLimitError(msg)
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


def _summary_output_token_limit(model: Model) -> int | None:
    claude_model = as_anthropic_claude(model)
    return claude_model.max_tokens if claude_model is not None else None


def _summary_response_likely_truncated(response: ModelResponse, *, output_token_limit: int | None) -> bool:
    if output_token_limit is None:
        return False
    output_tokens = _response_output_tokens(response)
    return output_tokens is not None and output_tokens >= output_token_limit


def _response_output_tokens(response: ModelResponse) -> int | None:
    if response.output_tokens is not None:
        return response.output_tokens
    if response.response_usage is None:
        return None
    return response.response_usage.output_tokens
