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
   the outer chunk timeout alone. Warm-prefix requests (``reuses_reply_prefix``)
   keep cache flags and thinking exactly as the reply path configured them, so
   the provider prompt cache built by the reply request stays valid.

4. Budget shrinks deterministically on provider failure.
   ``SummaryRetryPolicy`` decides which error classes warrant a smaller retry
   (timeouts and the named context-length fragments), the shrink schedule
   (halving), and the give-up floor — no inline string matching at call sites.

The request has exactly two shapes: the standalone two-message request from
``build_summary_request_messages``, or a pre-assembled ``SummaryProviderRequest``
built by ``mindroom.history.warm_prefix`` that reproduces the reply-path prefix.
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
    from agno.tools.function import Function

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


@dataclass(frozen=True)
class SummaryProviderRequest:
    """Pre-assembled provider request for one warm-prefix summary call.

    ``reuses_reply_prefix`` marks requests whose messages reproduce the active
    reply-path request prefix; configuration that would invalidate the provider
    prompt cache (cache flags, thinking) is preserved for those calls.
    """

    messages: tuple[Message, ...]
    tools: tuple[dict[str, object], ...] = ()
    tool_choice: str | dict[str, object] | None = None
    reuses_reply_prefix: bool = True


def configure_summary_model(
    model: Model,
    *,
    timeout_seconds: float | None = None,
    reuses_reply_prefix: bool = False,
) -> Model:
    """Apply all compaction-specific provider tuning to one loaded model (invariant 3).

    ``isinstance(model, Claude)`` covers the anthropic, vertexai_claude, and
    bedrock_claude providers because both forks subclass the Anthropic model.
    Mutating the instance is safe: ``get_model_instance`` builds a fresh model per
    call and compaction loads its own instance per run.

    When ``reuses_reply_prefix`` is true the request reproduces the reply-path
    prefix, so cache flags stay untouched (the cache_control breakpoints must
    match the reply request) and ``thinking`` stays as configured (toggling
    thinking invalidates message-level cache entries). Retry and timeout tuning
    applies to every summary call.
    """
    if not isinstance(model, Claude):
        logger.debug(
            "Compaction summary model tuning skipped",
            model_type=type(model).__name__,
            reason="provider_specific_tuning_only_defined_for_claude",
        )
        return model
    resolved_timeout = MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    if not reuses_reply_prefix:
        model.cache_system_prompt = False
        model.extended_cache_time = False
        model.thinking = None
    if model.thinking is None:
        # The output cap is a sampling parameter, not prefix content, so it never
        # invalidates the prompt cache; with thinking enabled, max_tokens must stay
        # above the configured thinking budget, so the reply-path value is kept.
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
    provider_request: SummaryProviderRequest | None = None,
) -> SessionSummary:
    """Issue one compaction summary call with tuned provider config and one timeout.

    Without ``provider_request`` the call sends the standalone two-message request
    from ``build_summary_request_messages``. With one, the pre-assembled
    warm-prefix request is sent verbatim (messages, tool schemas, tool_choice).
    """
    del timing_scope
    resolved_timeout = MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    configure_summary_model(
        model,
        timeout_seconds=resolved_timeout,
        reuses_reply_prefix=provider_request.reuses_reply_prefix if provider_request is not None else False,
    )

    async def _request_summary() -> ModelResponse:
        try:
            if provider_request is not None:
                request_tools: list[Function | dict] | None = (
                    [dict(tool) for tool in provider_request.tools] if provider_request.tools else None
                )
                return await model.aresponse(
                    messages=list(provider_request.messages),
                    tools=request_tools,
                    tool_choice=provider_request.tool_choice,
                )
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
