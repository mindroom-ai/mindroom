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
   constants in two modules. Gemini, OpenAI Chat, and Cerebras summary calls use
   one candidate so provider usage describes the same response Agno retains.
   Summary output uses the loaded model's configured output cap as the truncation
   guard. Gemini's guard includes its separately reported thinking tokens because
   they consume the same output budget. Unknown providers pass through untouched
   and rely on the outer chunk timeout alone.

4. Retry on provider failure is deterministic.
   ``SummaryRetryPolicy`` decides which error classes warrant a smaller retry
   (timeouts and the named context-length fragments), the shrink schedule
   (halving), and the give-up floor — no inline string matching at call sites.
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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from agno.models.message import Message
from agno.session.summary import SessionSummary

from mindroom.cancellation import request_task_cancel
from mindroom.claude_prompt_cache import as_anthropic_claude
from mindroom.constants import MINDROOM_COMPACTION_CHUNK_TIMEOUT_SECONDS
from mindroom.logging_config import get_logger
from mindroom.model_instance_checks import isinstance_of_loaded
from mindroom.timing import timed
from mindroom.token_budget import configured_model_max_output_tokens

if TYPE_CHECKING:
    from agno.models.base import Model
    from agno.models.cerebras import Cerebras
    from agno.models.google import Gemini
    from agno.models.openai import OpenAIChat
    from agno.models.response import ModelResponse

logger = get_logger(__name__)

_COMPACTION_CANCEL_DRAIN_TIMEOUT_SECONDS = 1.0
_CEREBRAS_CLASS = ("agno.models.cerebras.cerebras", "Cerebras")
_GOOGLE_GEMINI_CLASS = ("agno.models.google.gemini", "Gemini")
_OPENAI_CHAT_CLASS = ("agno.models.openai.chat", "OpenAIChat")

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


@runtime_checkable
class _ConfigWithCandidateCount(Protocol):
    """Typed surface for Gemini SDK request config objects."""

    candidate_count: int | None


@dataclass(frozen=True)
class SummaryRetryPolicy:
    """Explicit retry policy for failed compaction summary calls.

    The schedule is deterministic: each shrinkable failure divides the input
    budget by ``shrink_divisor`` (clamped to ``floor_tokens``). Ordinary errors
    stop at ``max_attempts``; empty successes stop at
    ``empty_result_max_attempts``.
    """

    max_attempts: int = 2
    empty_result_max_attempts: int = 3
    shrink_divisor: int = 2
    floor_tokens: int = 1_000

    def should_shrink(self, error: Exception) -> bool:
        """Return whether a smaller summary input may resolve this provider failure."""
        if isinstance(error, TimeoutError | CompactionSummaryOutputLimitError):
            return True
        message = str(error).lower()
        return any(fragment in message for fragment in _RETRYABLE_PROVIDER_ERROR_FRAGMENTS)

    def retry_budget(self, *, attempt: int, budget: int, error: Exception) -> int | None:
        """Return the input budget for the next attempt, or None when the policy gives up."""
        is_empty_result = isinstance(error, _CompactionSummaryEmptyResultError)
        max_attempts = self.empty_result_max_attempts if is_empty_result else self.max_attempts
        if attempt >= max_attempts:
            return None
        if not is_empty_result and not self.should_shrink(error):
            return None
        smaller_budget = max(self.floor_tokens, budget // self.shrink_divisor)
        if smaller_budget >= budget:
            return None
        return smaller_budget


DEFAULT_SUMMARY_RETRY_POLICY = SummaryRetryPolicy()


def _multiple_candidates_requested(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 1


def _force_single_gemini_candidate(model: Gemini) -> None:
    effective_config = model.get_request_params().get("config")
    if isinstance(effective_config, Mapping):
        candidate_count = effective_config.get("candidate_count")
    elif isinstance(effective_config, _ConfigWithCandidateCount):
        candidate_count = effective_config.candidate_count
    else:
        return
    if not _multiple_candidates_requested(candidate_count):
        return

    request_params = dict(model.request_params or {})
    if "config" not in request_params:
        generative_model_kwargs = dict(model.generative_model_kwargs or {})
        generative_model_kwargs["candidate_count"] = 1
        model.generative_model_kwargs = generative_model_kwargs
        return

    request_config = request_params["config"]
    if isinstance(request_config, Mapping):
        adjusted_config = dict(request_config)
        adjusted_config["candidate_count"] = 1
        request_params["config"] = adjusted_config
        model.request_params = request_params
    elif isinstance(request_config, _ConfigWithCandidateCount):
        request_config.candidate_count = 1


def _force_single_openai_chat_choice(model: OpenAIChat) -> None:
    effective_request = model.get_request_params()
    request_params = dict(model.request_params or {})
    changed = False
    if _multiple_candidates_requested(effective_request.get("n")):
        request_params["n"] = 1
        changed = True

    extra_body = effective_request.get("extra_body")
    if isinstance(extra_body, Mapping) and _multiple_candidates_requested(extra_body.get("n")):
        adjusted_extra_body = dict(extra_body)
        adjusted_extra_body["n"] = 1
        request_params["extra_body"] = adjusted_extra_body
        changed = True

    if changed:
        model.request_params = request_params


def _force_single_cerebras_choice(model: Cerebras) -> None:
    effective_request = model.get_request_params()
    request_params = dict(model.request_params or {})
    if _multiple_candidates_requested(effective_request.get("n")):
        request_params["n"] = 1
        model.request_params = request_params

    extra_body = effective_request.get("extra_body")
    if not isinstance(extra_body, Mapping) or not _multiple_candidates_requested(extra_body.get("n")):
        return
    adjusted_extra_body = dict(extra_body)
    adjusted_extra_body["n"] = 1
    if "extra_body" in request_params:
        request_params["extra_body"] = adjusted_extra_body
        model.request_params = request_params
    else:
        model.extra_body = adjusted_extra_body


def _force_single_summary_candidate(model: Model) -> None:
    """Keep provider usage and Agno's retained first response on the same scope."""
    if isinstance_of_loaded(model, _GOOGLE_GEMINI_CLASS):
        _force_single_gemini_candidate(cast("Gemini", model))
    elif isinstance_of_loaded(model, _OPENAI_CHAT_CLASS):
        _force_single_openai_chat_choice(cast("OpenAIChat", model))
    elif isinstance_of_loaded(model, _CEREBRAS_CLASS):
        _force_single_cerebras_choice(cast("Cerebras", model))


def configure_summary_model(model: Model, *, timeout_seconds: float | None = None) -> Model:
    """Apply all compaction-specific provider tuning to one loaded model (invariant 3).

    ``isinstance(model, Claude)`` covers the anthropic, vertexai_claude, and
    bedrock_claude providers because both forks subclass the Anthropic model.
    Mutating the instance is safe: ``get_model_instance`` builds a fresh model per
    call and compaction loads its own instance per run.
    """
    _force_single_summary_candidate(model)
    claude_model = as_anthropic_claude(model)
    if claude_model is None:
        logger.debug(
            "Compaction Claude-specific model tuning skipped",
            model_type=type(model).__name__,
            reason="model_is_not_claude",
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
    summary_output_limit = configured_model_max_output_tokens(configured_model)

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
    if _summary_response_likely_truncated(
        response,
        output_token_limit=summary_output_limit,
        include_reasoning_tokens=isinstance_of_loaded(configured_model, _GOOGLE_GEMINI_CLASS),
    ):
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


def _summary_response_likely_truncated(
    response: ModelResponse,
    *,
    output_token_limit: int | None,
    include_reasoning_tokens: bool = False,
) -> bool:
    if output_token_limit is None:
        return False
    output_tokens = _response_output_tokens(response)
    if output_tokens is None:
        return False
    reasoning_tokens = _response_reasoning_tokens(response) if include_reasoning_tokens else 0
    return output_tokens + (reasoning_tokens or 0) >= output_token_limit


def _response_output_tokens(response: ModelResponse) -> int | None:
    if response.output_tokens is not None:
        return response.output_tokens
    if response.response_usage is None:
        return None
    return response.response_usage.output_tokens


def _response_reasoning_tokens(response: ModelResponse) -> int | None:
    if response.reasoning_tokens is not None:
        return response.reasoning_tokens
    if response.response_usage is None:
        return None
    return response.response_usage.reasoning_tokens
