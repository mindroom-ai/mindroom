"""One compaction summary call: model tuning, request build, timeout, and retry policy.

This module is the only path for issuing a compaction summary model call.
It enforces the call-side half of the compaction invariants
(see ``tests/test_compaction_invariants.py``):

3. Summary calls get exactly one model configuration path.
   ``configure_summary_model`` applies all compaction-specific provider tuning in
   one place: prompt-cache writes off, Claude thinking cleared (mandatory whenever
   max_tokens is capped — a thinking budget at or above the cap is a 400 from
   Anthropic), summary output capped at ``SUMMARY_MAX_OUTPUT_TOKENS``, SDK retries
   disabled, and one SDK timeout coordinated with the outer chunk budget
   (``MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS``) instead of two uncoordinated
   constants in two modules. Unknown providers pass through untouched and rely on
   the outer chunk timeout alone.

4. Budget shrinks deterministically on provider failure.
   ``SummaryRetryPolicy`` decides which error classes warrant a smaller retry
   (timeouts and the named context-length fragments), the shrink schedule
   (halving), and the give-up floor — no inline string matching at call sites.

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

from agno.models.anthropic import Claude
from agno.models.message import Message
from agno.session.summary import SessionSummary

from mindroom.cancellation import request_task_cancel
from mindroom.constants import MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
from mindroom.logging_config import get_logger
from mindroom.timing import timed

if TYPE_CHECKING:
    from agno.models.base import Model
    from agno.models.response import ModelResponse

logger = get_logger(__name__)

SUMMARY_MAX_OUTPUT_TOKENS = 4096
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
_OUTPUT_LIMIT_FINISH_REASONS = frozenset(
    {
        "length",
        "max_tokens",
        "max_output_tokens",
        "model_length",
        "token_limit",
        "stop_max_tokens",
    },
)
_OUTPUT_LIMIT_REASON_KEYS = frozenset(
    {
        "finish_reason",
        "finishReason",
        "finish_message",
        "finishMessage",
        "stop_reason",
        "stopReason",
        "completion_reason",
        "completionReason",
    },
)


@dataclass(frozen=True)
class SummaryRetryPolicy:
    """Explicit budget-shrink policy for failed compaction summary calls.

    The schedule is deterministic: each policy-approved failure divides the input
    budget by ``shrink_divisor`` (clamped to ``floor_tokens``); once the budget can
    no longer shrink, or ``max_attempts`` is reached, the error propagates.
    """

    max_attempts: int = 2
    shrink_divisor: int = 2
    floor_tokens: int = 1_000

    def should_shrink(self, error: Exception) -> bool:
        """Return whether a smaller summary input may resolve this provider failure."""
        if isinstance(error, TimeoutError):
            return True
        message = str(error).lower()
        return any(fragment in message for fragment in _RETRYABLE_PROVIDER_ERROR_FRAGMENTS)

    def retry_budget(self, *, attempt: int, budget: int, error: Exception) -> int | None:
        """Return the next smaller input budget, or None when the policy gives up."""
        if attempt >= self.max_attempts or not self.should_shrink(error):
            return None
        smaller_budget = max(self.floor_tokens, budget // self.shrink_divisor)
        if smaller_budget >= budget:
            return None
        return smaller_budget


DEFAULT_SUMMARY_RETRY_POLICY = SummaryRetryPolicy()


def configure_summary_model(model: Model, *, timeout_seconds: float | None = None) -> Model:
    """Apply all compaction-specific provider tuning to one loaded model (invariant 3).

    ``isinstance(model, Claude)`` covers the anthropic, vertexai_claude, and
    bedrock_claude providers because both forks subclass the Anthropic model.
    Mutating the instance is safe: ``get_model_instance`` builds a fresh model per
    call and compaction loads its own instance per run.
    """
    if not isinstance(model, Claude):
        logger.debug(
            "Compaction summary model tuning skipped",
            model_type=type(model).__name__,
            reason="provider_specific_tuning_only_defined_for_claude",
        )
        return model
    resolved_timeout = MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    model.cache_system_prompt = False
    model.extended_cache_time = False
    model.thinking = None
    model.max_tokens = (
        min(model.max_tokens, SUMMARY_MAX_OUTPUT_TOKENS) if model.max_tokens else SUMMARY_MAX_OUTPUT_TOKENS
    )
    model.timeout = min(model.timeout, resolved_timeout) if model.timeout else resolved_timeout
    client_params = dict(model.client_params or {})
    client_params["max_retries"] = 0
    model.client_params = client_params
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
    timing_scope: str | None = None,
) -> SessionSummary:
    """Issue one compaction summary call with tuned provider config and one timeout."""
    del timing_scope
    resolved_timeout = MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    configure_summary_model(model, timeout_seconds=resolved_timeout)

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
        msg = "summary generation returned no result"
        raise RuntimeError(msg)
    if _summary_response_likely_truncated(response, normalized_text):
        msg = "compaction summary likely truncated at max tokens; refusing to persist incomplete summary"
        raise RuntimeError(msg)
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


def _summary_response_likely_truncated(response: ModelResponse, normalized_text: str) -> bool:
    if _response_reports_output_limit(response):
        return True
    output_tokens = _response_output_tokens(response)
    if output_tokens is None or output_tokens < SUMMARY_MAX_OUTPUT_TOKENS:
        return False
    return _summary_ends_mid_token(normalized_text)


def _response_output_tokens(response: ModelResponse) -> int | None:
    if response.output_tokens is not None:
        return response.output_tokens
    if response.response_usage is None:
        return None
    return response.response_usage.output_tokens


def _response_reports_output_limit(response: ModelResponse) -> bool:
    return any(_payload_reports_output_limit(payload) for payload in (response.provider_data, response.extra))


def _payload_reports_output_limit(payload: object) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in _OUTPUT_LIMIT_REASON_KEYS and _is_output_limit_reason(value):
                return True
            if _payload_reports_output_limit(value):
                return True
    if isinstance(payload, list):
        return any(_payload_reports_output_limit(item) for item in payload)
    return False


def _is_output_limit_reason(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
    return normalized in _OUTPUT_LIMIT_FINISH_REASONS


def _summary_ends_mid_token(text: str) -> bool:
    return bool(text) and (text[-1].isalnum() or text[-1] in {"/", "_", "-"})
