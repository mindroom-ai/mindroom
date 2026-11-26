"""Fall back across a configured chain of model providers when a provider cannot serve a request right now.

The chain wraps the full request pipeline of each model, so every model keeps its own retries, media fallback,
idle limit, and request log. A failure after the caller received output or a hosted tool committed is final, as is
any failure another model would repeat (invalid request, context-window overflow, safeguard refusal, cancellation).
A failure that names when its limit resets moves that route to the end of the chain until then; an uncommitted
``ProviderUnavailable`` failure without a reset hint cools that route down for a short default window.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.llm.errors import ProviderError, ProviderUnavailable, fails_over
from mindroom.llm.messages import FALLBACK_MODEL_KEY, REWRITTEN_ATTEMPT_KEY, Message
from mindroom.llm.provider import (
    DeferredTools,
    ModelSelected,
    ReportedUsage,
    StructuredOutput,
    TurnCompleted,
    json_output_prompt,
)
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.connectivity import CONNECTIVITY_ERROR_CODES
from mindroom.providers.pipeline.wrapped import commits_attempt
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from mindroom.llm.provider import ModelProvider, ModelRequest, StreamEvent

__all__ = ["ChainModel", "fallback_stream"]

logger = get_logger(__name__)

# When each route/account fingerprint may be tried again, on the ``time.monotonic`` clock.
_COOLDOWN_UNTIL: dict[str, float] = {}
_UNAVAILABLE_COOLDOWN_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class ChainModel:
    """One fallback model: its configured reference, context window, provider builder, and cooldown key."""

    name: str
    context_window: int | None
    build: Callable[[], ModelProvider]
    cooldown_key: str


def _follows_fallback(messages: Sequence[Message]) -> bool:
    """Return whether the latest assistant message in ``messages`` was answered by a fallback model."""
    for message in reversed(messages):
        if message.role == "assistant":
            return FALLBACK_MODEL_KEY in (message.provider_data or {})
    return False


def _primary_request(request: ModelRequest) -> ModelRequest:
    """Return ``request`` for the primary model, making continuation portable when following a fallback turn.

    Keeps the primary's own ``native_compaction`` checkpoint intact: foreign Responses IDs and encrypted items are
    already rejected per message via ``FALLBACK_MODEL_KEY``.
    """
    return replace(request, portable_replay=True) if _follows_fallback(request.messages) else request


def _fallback_request(request: ModelRequest, *, model_name: str) -> ModelRequest:
    """Return ``request`` without the state only the first model's route can replay."""
    return replace(
        request,
        native_compaction=None,
        portable_replay=True,
        strip_encrypted_replay=True,
        fallback_model=model_name,
        retry_budget=None,
        prompt_hold=None,
        prompt_hold_pin=None,
    )


def _with_structured_output_prompt(
    request: ModelRequest,
    *,
    from_mode: StructuredOutput,
    to_mode: StructuredOutput,
) -> ModelRequest:
    """Adjust the JSON-mode schema instruction when switching between JSON-mode and schema-enforcing routes."""
    if request.output_schema is None or (from_mode == "json_mode") == (to_mode == "json_mode"):
        return request
    prompt = json_output_prompt(request.output_schema)
    messages = list(request.messages)
    if to_mode == "json_mode":
        if messages and messages[0].role == "system" and isinstance(messages[0].content, str):
            existing = messages[0].content
            messages[0] = replace(messages[0], content=f"{existing}\n\n{prompt}" if existing else prompt)
        else:
            messages.insert(0, Message(role="system", content=prompt))
    elif messages and messages[0].role == "system" and isinstance(messages[0].content, str):
        existing = messages[0].content
        if existing == prompt:
            messages.pop(0)
        elif existing.endswith(f"\n\n{prompt}"):
            messages[0] = replace(messages[0], content=existing.removesuffix(f"\n\n{prompt}"))
    return replace(request, messages=tuple(messages))


def _remember_reset(key: str, error: ProviderError, *, committed: bool = False) -> None:
    if (hint := error.retry_after_seconds) is not None and hint > 0:
        cooldown_seconds = hint
    elif not committed and isinstance(error, ProviderUnavailable):
        cooldown_seconds = _UNAVAILABLE_COOLDOWN_SECONDS
    else:
        return
    until = time.monotonic() + cooldown_seconds
    _COOLDOWN_UNTIL[key] = max(_COOLDOWN_UNTIL.get(key, 0.0), until)


def _clear_cooldown(key: str, seen_until: float | None) -> None:
    if _COOLDOWN_UNTIL.get(key) == seen_until:
        _COOLDOWN_UNTIL.pop(key, None)


def _is_connectivity_error(error: ProviderError) -> bool:
    return isinstance(error, ProviderUnavailable) and error.code in CONNECTIVITY_ERROR_CODES


def _restore_cooldowns(touched: dict[str, float | None], *, all_connectivity_errors: bool) -> None:
    if not all_connectivity_errors:
        return
    for key, prior_until in touched.items():
        if prior_until is None:
            _COOLDOWN_UNTIL.pop(key, None)
        else:
            _COOLDOWN_UNTIL[key] = prior_until


def _log_fallback(failure: tuple[str, ProviderError] | None, *, to_model: str) -> None:
    if failure is None:
        return
    from_model, error = failure
    logger.warning(
        "model_fallback_used",
        from_model=from_model,
        to_model=to_model,
        error_type=type(error).__name__,
        error_code=error.code,
        status_code=error.status_code,
        provider=error.provider,
        error=redact_sensitive_text(str(error), max_length=500),
    )


def _fits(provider: ModelProvider, request: ModelRequest, context_window: int | None) -> bool:
    return context_window is None or provider.request_tokens(request) <= context_window


async def _sendable(
    member: ChainModel,
    request: ModelRequest,
    *,
    primary_structured_output: StructuredOutput,
) -> tuple[ModelProvider, ModelRequest] | None:
    """Return a fallback's provider and compatible request, or None when it cannot take the request.

    A fallback that cannot be built or sized, such as one whose SDK is missing, is skipped like one the request does
    not fit, so the chain goes on and the last provider failure stays the one reported.
    """
    try:
        provider = await asyncio.to_thread(member.build)
        sent = _fallback_request(request, model_name=member.name)
        if (
            sent.deferred_tools.names
            and not sent.deferred_tools.portable
            and not provider.capabilities.native_tool_search
        ):
            sent = replace(sent, deferred_tools=DeferredTools())
        sent = _with_structured_output_prompt(
            sent,
            from_mode=primary_structured_output,
            to_mode=provider.capabilities.structured_output,
        )
        fits = await asyncio.to_thread(_fits, provider, sent, member.context_window)
    except Exception as error:
        logger.warning(
            "model_fallback_skipped",
            model=member.name,
            reason="unavailable",
            error_type=type(error).__name__,
            error=redact_sensitive_text(str(error), max_length=500),
        )
        return None
    if not fits:
        logger.warning("model_fallback_skipped", model=member.name, reason="context_window")
        return None
    return provider, sent


def _in_turn(
    primary: ChainModel,
    chain: Sequence[ChainModel],
    *,
    allow_fallback: Callable[[str], bool] | None,
) -> list[ChainModel]:
    """Return ``primary`` and eligible ``chain`` members in order, those cooling down after the rest."""
    members = [primary, *(member for member in chain if allow_fallback is None or allow_fallback(member.name))]
    now = time.monotonic()
    cooling = [member for member in members if _COOLDOWN_UNTIL.get(member.cooldown_key, 0.0) > now]
    if cooling:
        logger.debug("model_fallback_cooldown", model=primary.name, cooling=[member.name for member in cooling])
    return [member for member in members if member not in cooling] + cooling


async def fallback_stream(
    inner: ModelProvider,
    request: ModelRequest,
    *,
    model_name: str,
    chain: Sequence[ChainModel],
    cooldown_key: str,
    allow_fallback: Callable[[str], bool] | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream ``request`` on ``inner`` (``model_name``), then on eligible ``chain`` members while models fail over.

    With ``transient_retries="caller"`` (such as compaction summaries), retryable errors defer to the caller's retry
    loop while non-retryable availability errors (quota, credentials) fail over immediately; ``"fallback"`` advances
    immediately on retryable errors too. Fallback turns are marked so later requests never reuse foreign continuation
    or encrypted replay state.
    """
    primary = ChainModel(model_name, None, lambda: inner, cooldown_key)
    members = _in_turn(primary, chain, allow_fallback=allow_fallback)
    # A success clears only the cooldown this request saw, not one a concurrent request set since.
    seen = {member.cooldown_key: _COOLDOWN_UNTIL.get(member.cooldown_key) for member in members}
    touched: dict[str, float | None] = {}
    all_connectivity_errors = True
    failure: tuple[str, ProviderError] | None = None
    for member in members:
        is_fallback = member is not primary
        target = (
            await _sendable(member, request, primary_structured_output=inner.capabilities.structured_output)
            if is_fallback
            else (inner, _primary_request(request))
        )
        if target is None:
            continue
        provider, sent = target
        _log_fallback(failure, to_model=member.name)
        committed = False
        usage = ReportedUsage()
        try:
            yield ModelSelected(identity=provider.identity, model_name=member.name, capabilities=provider.capabilities)
            events = provider.stream(sent)
            async with closing_async_stream(events):
                async for event in events:
                    usage.observe(event)
                    committed = committed or commits_attempt(event)
                    if is_fallback and isinstance(event, TurnCompleted):
                        message = event.message
                        message.provider_data = {
                            **(message.provider_data or {}),
                            REWRITTEN_ATTEMPT_KEY: True,
                            FALLBACK_MODEL_KEY: member.name,
                        }
                    yield event
        except ProviderError as error:
            defer_transient = request.overrides.transient_retries == "caller" and error.retryable
            can_fail_over = fails_over(error) and not defer_transient
            if can_fail_over:
                touched.setdefault(member.cooldown_key, seen[member.cooldown_key])
                _remember_reset(member.cooldown_key, error, committed=committed)
            if committed or not can_fail_over:
                raise
            all_connectivity_errors = all_connectivity_errors and _is_connectivity_error(error)
            failure = (member.name, error)
        else:
            _clear_cooldown(member.cooldown_key, seen[member.cooldown_key])
            return
        if (report := usage.settle()) is not None:
            yield report
    # The request's own model is never skipped, so a failure ends the chain.
    assert failure is not None
    _restore_cooldowns(touched, all_connectivity_errors=all_connectivity_errors)
    raise failure[1]
