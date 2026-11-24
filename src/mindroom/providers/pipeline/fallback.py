"""Answer a request with the next model of its fallback chain when its model's provider cannot serve it now.

This stage wraps the full pipelines of the chain's models, so each model gets its own retries, media fallback, and
request log before the next one is tried. Only a failure that says the provider cannot serve now fails over: a quota
or usage limit, an outage or rate limit that outlasted the retries, or credentials that are missing or rejected. A
failure that any model would repeat, or that the request caused, never does: an invalid request, a context window
the request does not fit, a safeguard refusal, or a cancellation.

A request fails over only while the caller has received no output of it, neither text nor a tool call nor a hosted
tool that acts, the same commit rule the retry stage keeps (``wrapped.commits_attempt``), so nothing the caller showed
or ran is repeated. A model whose failure named when it resets (``ProviderError.retry_after_seconds``) is tried
after the others until then, so a usage limit that lasts hours is not probed first on every request.

A fallback model is sent the request without the provider-specific state the history planned for the first model:
no native compaction, no provider-encrypted replay, and no held system prompt. History is budgeted for the first
model's context window, so a fallback with a configured window the request does not fit is skipped, as is one that
cannot be built.

A request with an output schema is shaped by its caller for the first model's structured-output mode
(``Capabilities.structured_output``, which the chain advertises). A fallback with another mode gets the JSON prompt
when it relies on JSON mode, and its answer reaches the caller as text holding the schema's JSON, repaired when it
came from JSON mode, which every mode's reader (``agent_loop/structured.py``) parses.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.structured import json_output_prompt, parse_structured, structured_answer_text
from mindroom.llm.errors import (
    QUOTA_EXHAUSTED,
    TOKENS_PER_MINUTE,
    AuthenticationFailed,
    ContextWindowExceeded,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.messages import REWRITTEN_ATTEMPT_KEY, Message
from mindroom.llm.provider import (
    STRUCTURED_OUTPUT_TOOL,
    ModelTurn,
    ReportedUsage,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.usage import add_usage
from mindroom.logging_config import get_logger
from mindroom.providers.pipeline.wrapped import commits_attempt, with_carried_usage
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import closing_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from pydantic import BaseModel

    from mindroom.llm.provider import Capabilities, ModelProvider, ModelRequest, StreamEvent, StructuredOutput
    from mindroom.llm.usage import MessageMetrics

__all__ = ["ChainModel", "fails_over", "fallback_stream"]

logger = get_logger(__name__)

# Statuses of a final ``ProviderError`` whose route already retried the request itself, such as Claude Code's.
_TRANSIENT_STATUSES = frozenset({408, 429, *range(500, 600)})

# When each configured model may be tried again, on the ``time.monotonic`` clock; process-wide like the routes' limits.
_COOLDOWN_UNTIL: dict[str, float] = {}


@dataclass(frozen=True, slots=True)
class ChainModel:
    """One fallback model: its configured name, its context window, and how to build its provider on first use."""

    name: str
    context_window: int | None
    build: Callable[[], ModelProvider]


def fails_over(error: ProviderError) -> bool:
    """Return whether ``error`` means the provider cannot serve now, so another model may answer the request."""
    if error.code in {QUOTA_EXHAUSTED, TOKENS_PER_MINUTE}:
        return True
    if isinstance(error, ContextWindowExceeded):
        return False
    if isinstance(error, RateLimited | ProviderUnavailable | AuthenticationFailed):
        return True
    return type(error) is ProviderError and error.code is None and error.status_code in _TRANSIENT_STATUSES


def _fallback_request(request: ModelRequest) -> ModelRequest:
    """Return ``request`` without the state only the first model's route can replay."""
    return replace(
        request,
        native_compaction=None,
        portable_replay=True,
        strip_encrypted_replay=True,
        retry_budget=None,
        prompt_hold=None,
        prompt_hold_pin=None,
    )


def _cooling(name: str, now: float) -> bool:
    return _COOLDOWN_UNTIL.get(name, 0.0) > now


def _remember_reset(name: str, error: ProviderError) -> None:
    if (hint := error.retry_after_seconds) is not None and hint > 0:
        _COOLDOWN_UNTIL[name] = time.monotonic() + hint


def _log_fallback(error: ProviderError, *, from_model: str, to_model: str) -> None:
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


def fallback_stream(
    inner: ModelProvider,
    request: ModelRequest,
    *,
    model_name: str,
    chain: Sequence[ChainModel],
) -> AsyncIterator[StreamEvent]:
    """Stream ``request`` on ``inner``, the model ``model_name``, then on each model of ``chain`` while they fail over.

    A request whose caller retries it itself passes through unchanged, so that caller keeps its own fallback. The
    last failure propagates once every model failed over. Usage of the failed models is forwarded and summed into the
    answer's finished turn, as across retries, and that turn is marked as answering another request than the one sent.
    """
    if request.overrides.caller_retries:
        return inner.stream(request)
    return _chain_stream(inner, request, model_name=model_name, chain=chain)


@dataclass(slots=True)
class _Attempt:
    """What one model's stream handed the caller so far: whether it committed, and the usage it reported."""

    committed: bool = False
    usage: ReportedUsage = field(default_factory=ReportedUsage)


@dataclass(frozen=True, slots=True)
class _ModeShift:
    """A structured request the caller shaped for ``expected``, answered by a fallback that enforces ``answering``."""

    schema: type[BaseModel]
    expected: StructuredOutput
    answering: Capabilities


def _mode_shift(request: ModelRequest, expected: StructuredOutput, answering: Capabilities) -> _ModeShift | None:
    if request.output_schema is None or answering.structured_output == expected:
        return None
    return _ModeShift(request.output_schema, expected, answering)


def _shifted_request(request: ModelRequest, shift: _ModeShift) -> ModelRequest:
    """Return ``request`` with the JSON prompt a JSON-mode fallback needs, which the caller added only for its mode."""
    if shift.answering.structured_output != "json_mode" or shift.expected == "json_mode":
        return request
    prompt = json_output_prompt(shift.schema)
    messages = list(request.messages)
    first = messages[0] if messages else None
    if first is None or first.role != "system":
        messages.insert(0, Message(role="system", content=prompt))
    elif isinstance(first.content, str):
        messages[0] = replace(first, content=f"{first.content.rstrip()}\n\n{prompt}")
    else:
        messages[0] = replace(first, content=[*(first.content or []), {"type": "text", "text": prompt}])
    return replace(request, messages=tuple(messages))


def _shifted_turn(event: TurnCompleted, shift: _ModeShift) -> TurnCompleted:
    """Return the fallback's structured answer in the shape the caller reads for its mode.

    The answer goes into the message text, where every mode's reader finds it once no forced call is left, and a
    JSON-mode answer is repaired into the schema's JSON for a caller that parses strictly.
    """
    message = event.message
    text = structured_answer_text(ModelTurn(message=message, stop_reason=event.stop_reason), shift.answering)
    if shift.answering.structured_output == "json_mode" and shift.expected != "json_mode":
        parsed = parse_structured(text, shift.schema, lenient=True)
        text = text if parsed is None else parsed.model_dump_json()
    calls = [call for call in message.tool_calls or () if _call_name(call) != STRUCTURED_OUTPUT_TOOL]
    message.content, message.tool_calls = text, calls or None
    return replace(event, stop_reason="end") if event.stop_reason == "tool_use" and not calls else event


def _call_name(call: Mapping[str, Any]) -> object:
    return (call.get("function") or {}).get("name")


async def _attempt_stream(
    provider: ModelProvider,
    sent: ModelRequest,
    *,
    fallback: bool,
    shift: _ModeShift | None,
    carried: MessageMetrics | None,
    attempt: _Attempt,
) -> AsyncIterator[StreamEvent]:
    """Stream one model's attempt, recording in ``attempt`` what it handed the caller.

    A fallback's usage names the fallback, which billed it, and its finished turn answers another request; a
    structured answer in another mode reaches the caller in the shape of the mode it asked for.
    """
    events = provider.stream(sent)
    async with closing_async_stream(events):
        async for event in events:
            attempt.usage.observe(event)
            attempt.committed = attempt.committed or commits_attempt(event)
            if fallback and isinstance(event, UsageReported):
                event = replace(event, answered_by=provider.identity)  # noqa: PLW2901
            # Text deltas of a shifted answer stay raw, since no reader of a structured request reads its deltas.
            elif fallback and isinstance(event, TurnCompleted):
                message = event.message
                message.provider_data = {**(message.provider_data or {}), REWRITTEN_ATTEMPT_KEY: True}
                event = event if shift is None else _shifted_turn(event, shift)  # noqa: PLW2901
            elif (
                shift is not None
                and isinstance(event, ToolCallReady)
                and _call_name(event.call) == STRUCTURED_OUTPUT_TOOL
            ):
                # The forced call becomes the answer text, so the caller never sees it as a tool call.
                continue
            yield with_carried_usage(event, carried)


async def _sendable(
    member: ChainModel,
    request: ModelRequest,
    expected: StructuredOutput,
) -> tuple[ModelProvider, ModelRequest, _ModeShift | None] | None:
    """Return a fallback's provider, the request it is sent, and its mode shift, or None when it cannot take it.

    A fallback that cannot be built or sized, such as one whose SDK is missing, is skipped like one the request does
    not fit, so the chain goes on and the last provider failure stays the one reported.
    """
    try:
        provider = await asyncio.to_thread(member.build)
        shift = _mode_shift(request, expected, provider.capabilities)
        sent = _fallback_request(request)
        sent = sent if shift is None else _shifted_request(sent, shift)
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
    return provider, sent, shift


def _in_turn(members: Sequence[ChainModel], *, model_name: str) -> list[ChainModel]:
    """Return ``members`` in order, those cooling down after the rest, as a last resort."""
    now = time.monotonic()
    cooling = [member for member in members if _cooling(member.name, now)]
    if cooling:
        logger.debug("model_fallback_cooldown", model=model_name, cooling=[member.name for member in cooling])
    return [member for member in members if member not in cooling] + cooling


async def _chain_stream(
    inner: ModelProvider,
    request: ModelRequest,
    *,
    model_name: str,
    chain: Sequence[ChainModel],
) -> AsyncIterator[StreamEvent]:
    members = _in_turn([ChainModel(model_name, None, lambda: inner), *chain], model_name=model_name)
    # A success clears only the cooldown this request saw, not one a concurrent request set since.
    seen = {member.name: _COOLDOWN_UNTIL.get(member.name) for member in members}
    carried: MessageMetrics | None = None
    failure: tuple[str, ProviderError] | None = None
    for index, member in enumerate(members):
        expected = inner.capabilities.structured_output
        own = (inner, request, None)
        target = own if member.name == model_name else await _sendable(member, request, expected)
        if target is None:
            continue
        provider, sent, shift = target
        if failure is not None:
            _log_fallback(failure[1], from_model=failure[0], to_model=member.name)
        attempt = _Attempt()
        stream = _attempt_stream(
            provider,
            sent,
            fallback=provider is not inner,
            shift=shift,
            carried=carried,
            attempt=attempt,
        )
        try:
            async with closing_async_stream(stream):
                async for event in stream:
                    yield event
        except ProviderError as error:
            # The request's own model, demoted for its cooldown, still gets its turn before a fallback's failure ends it.
            own_left = any(later.name == model_name for later in members[index + 1 :])
            if attempt.committed or not (fails_over(error) or own_left):
                raise
            _remember_reset(member.name, error)
            failure = (member.name, error)
        else:
            if _COOLDOWN_UNTIL.get(member.name) == seen[member.name]:
                _COOLDOWN_UNTIL.pop(member.name, None)
            return
        if (report := attempt.usage.settle()) is not None:
            answered_by = None if sent is request else provider.identity
            yield with_carried_usage(replace(report, answered_by=answered_by), carried)
        carried = add_usage(carried, attempt.usage.settled)
    # The request's own model is never skipped, so a failure ends the chain.
    assert failure is not None
    raise failure[1]
