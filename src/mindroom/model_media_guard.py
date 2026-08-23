"""Wire-level guard for media the model replays from history or a tool produces mid-loop.

``ai.py`` and ``teams.py`` own the media the user attached to the current turn:
they built that run input, so they can retry it without media and tell the user
what was dropped. Neither can see the rest of the outgoing request. Agno replays
media from the persisted session on its own, and a tool can append an image to
the conversation halfway through the model loop, so the complete wire payload
first exists at the final async model call. That is the only boundary where this
guard can act, so it installs on ``ainvoke``/``ainvoke_stream``.

The guard is not a classifier: no provider error text reliably attributes a
failure to media. It runs a controlled experiment instead — the same request,
one variable removed — and obeys one invariant:

    A learned conclusion may not exceed its evidence, and a capability cache may
    only gate inputs of the same provenance class as the experiment that taught
    it.

Concretely: it acts only on failures the provider itself reported about this
request (transient outages and rate limits belong to the retry ladders in
``claude_stream_retry`` and ``Model._ainvoke_with_retry``), only when it owns
every piece of media on the wire (otherwise the outer layer is the retry owner
and would collide), removes one more kind per attempt so every conclusion has a
single changed variable behind it, waits for a second experiment before caching
one, and writes only the replayed-media cache — never the one that gates fresh
user uploads.

Scope is deliberately async-only. Every MindRoom agent and team path reaches the
provider through ``ainvoke``/``ainvoke_stream`` (``agno/models/base.py``), so
wrapping the sync twins would ship a second untested implementation.
"""

from __future__ import annotations

from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache, partial
from typing import TYPE_CHECKING, Any, cast

from agno.exceptions import ModelProviderError, ModelRateLimitError
from agno.models.message import Message

from mindroom.error_handling import TRANSIENT_PROVIDER_STATUS_CODES, is_model_safeguard_refusal
from mindroom.logging_config import get_logger
from mindroom.media_fallback import (
    build_model_media_route,
    capability_teaching_blocked,
    message_media_kinds,
    note_wire_media_recovery,
    record_replayed_media_isolation,
    strip_media_kinds_from_message,
    unsupported_replayed_media_kinds_for_route,
)
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import close_async_stream, context_bound_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Iterator

    from agno.models.base import Model
    from agno.models.response import ModelResponse

    from mindroom.media_fallback import ModelMediaRoute
    from mindroom.media_inputs import MediaKind

logger = get_logger(__name__)

__all__ = ["install_model_media_guard"]

_GUARD_INSTALLED_ATTR = "_mindroom_model_media_guard_installed"
_OMITTED_MEDIA_NOTE_TEMPLATE = (
    "[Attachment omitted: {kinds} content could not be sent with this request. Say so if your answer depends on it.]"
)
_MAX_LOGGED_ERROR_CHARS = 500
# A 4xx says the provider read the request and rejected the request itself.
_CLIENT_ERROR_STATUS_CODES = range(400, 500)
# The client errors that answer who is asking rather than what was asked: a
# rejected key and a forbidden resource are the same answer for every payload.
_CALLER_REJECTED_STATUS_CODES = frozenset({401, 403})

# Keyed by id(model): a model whose guarded method delegates to another guarded
# method on itself (``CodexResponses.ainvoke`` consumes its own
# ``ainvoke_stream``) must not open a second retry owner over one request.
_ACTIVE_GUARD_MODELS: ContextVar[frozenset[int]] = ContextVar(
    "mindroom_active_media_guard_models",
    default=frozenset(),
)
# Keyed by id(model): the non-history messages that existed when one model/tool
# loop started. Everything else reaching the wire is replayed or tool-produced.
# Concurrent turns share a ContextVar but never a model instance, so the key
# keeps one agent's current-turn attachments out of another's provenance view.
_OUTER_MESSAGE_IDS: ContextVar[dict[int, frozenset[str]] | None] = ContextVar(
    "mindroom_media_guard_outer_message_ids",
    default=None,
)


@dataclass(frozen=True, slots=True)
class _MediaAttempt:
    """One rung of the ladder: what comes off the wire, and what a success there would prove."""

    removed_kinds: frozenset[MediaKind]
    isolated_kind: MediaKind | None
    fills_an_empty_turn: bool


@dataclass(frozen=True, slots=True)
class _GuardedRequest:
    """One final provider request, classified by which layer owns its media."""

    messages: list[Message]
    route: ModelMediaRoute
    cached_kinds: frozenset[MediaKind]
    guard_owned_kinds: frozenset[MediaKind]
    outer_owns_media: bool
    outer_message_ids: frozenset[str] | None
    # The media each guard-owned message with no text of its own carries, read
    # once before anything is substituted. The ladder is planned from the request
    # as the caller passed it, and a rung's own note rewrites the very messages
    # a re-derivation would read, so nothing about the plan may depend on the
    # live list.
    blank_turn_media_kinds: tuple[frozenset[MediaKind], ...]

    def owns(self, message: Message) -> bool:
        """Report whether this message's media is the guard's to strip."""
        return _guard_owns_message(message, outer_message_ids=self.outer_message_ids)

    @property
    def may_retry(self) -> bool:
        """Retry only when the guard owns every piece of media left on the wire.

        While the outer fallback still has media of its own in the request it is
        the retry owner for this turn; a guard retry would race it and re-upload
        the attachment the provider just rejected.
        """
        return bool(self.guard_owned_kinds) and not self.outer_owns_media

    def attempts(self) -> list[_MediaAttempt]:
        """Plan the whole ladder as absolute removal sets, one rung per attempt.

        The first rung is the request as the cache already knows it should go
        out; each later rung takes off one more kind. Consecutive rungs
        therefore differ by exactly one kind, so a rung that succeeds where its
        predecessor failed isolates that kind as the cause and the request keeps
        the media no experiment has implicated yet. Removing everything at once
        would only ever prove that *some* kind was unwelcome, which is a
        conclusion the cache cannot store and the next turn would have to
        rediscover, paying a doomed call forever.

        Absolute sets rather than deltas layered by nesting: the blocking and
        streaming drivers each apply exactly one strip per attempt, from one
        plan they walk positionally, so a change to the ladder cannot land on
        one path only.
        """
        ordered = sorted(self.guard_owned_kinds)
        plan: list[_MediaAttempt] = []
        previous_kinds: frozenset[MediaKind] = frozenset()
        for isolated_kind, removed_kinds in [
            (None, self.cached_kinds),
            *((kind, self.cached_kinds | frozenset(ordered[: index + 1])) for index, kind in enumerate(ordered)),
        ]:
            plan.append(
                _MediaAttempt(
                    removed_kinds=removed_kinds,
                    isolated_kind=isolated_kind,
                    fills_an_empty_turn=self._fills_an_empty_turn(previous_kinds, removed_kinds),
                ),
            )
            previous_kinds = removed_kinds
        return plan

    def attempts_after(
        self,
        attempt: _MediaAttempt,
        remaining: list[_MediaAttempt],
        error: Exception,
        *,
        produced: bool,
    ) -> list[_MediaAttempt]:
        """Say what is left to try after this rung failed, or nothing to stop on.

        The guard gives up when a stream already reached its consumer (it cannot
        be replayed), when the outer fallback still owns media on the wire (it is
        the retry owner for this turn), when the failure is not the provider
        rejecting this request, and when this rung already sent everything the
        guard owns off the wire.

        Otherwise the discriminator is what the failure can teach. Isolating one
        kind per rung exists to buy an attributable, cacheable conclusion; when
        the failure cannot produce one — a context overflow, a per-attachment
        size limit, a blip — the rest of the ladder is k near-full-size uploads
        that prove nothing and get paid again next turn, because nothing was
        learned. Those collapse into a single attempt that removes everything the
        guard owns: the largest reduction, the likeliest to fit, at a fixed cost
        of two calls whatever k is.

        Whatever survives that, the answer is the caller's own untouched tail of
        the one plan. The ladder is never re-derived mid-walk: a rung is a
        position in a plan, not a value to look back up.
        """
        if produced or not self.may_retry or not _media_failure_is_retryable(error):
            return []
        if self.guard_owned_kinds <= attempt.removed_kinds:
            return []
        if capability_teaching_blocked(error):
            removed_kinds = self.cached_kinds | self.guard_owned_kinds
            return [
                _MediaAttempt(
                    removed_kinds=removed_kinds,
                    isolated_kind=None,
                    fills_an_empty_turn=self._fills_an_empty_turn(attempt.removed_kinds, removed_kinds),
                ),
            ]
        return remaining

    def _fills_an_empty_turn(
        self,
        previous_kinds: frozenset[MediaKind],
        removed_kinds: frozenset[MediaKind],
    ) -> bool:
        """Report whether this rung is the one that gives an otherwise empty message its note.

        The note is a second changed variable: a provider that rejects a turn
        carrying no content can accept the same turn once the note fills it, so
        a success on that rung is not evidence about the media. Removal sets
        only grow along the ladder, so a message gains its note exactly once and
        only that rung is confounded.
        """
        return any((kinds & removed_kinds) and not (kinds & previous_kinds) for kinds in self.blank_turn_media_kinds)


@dataclass(slots=True)
class _StreamAttempt:
    """What one guarded stream attempt produced before it stopped.

    ``produced`` and ``answered`` are two different facts and gate two different
    decisions. Any chunk at all reaching the consumer makes the attempt
    un-retryable — it cannot be replayed — which is what ``produced`` says. Only
    a chunk carrying visible content or a tool call makes it an *answer*, which
    is what may be learned from. An empty completion produces chunks and answers
    nothing: agno's OpenAI adapter emits a role-only first delta and a usage-only
    final chunk either way.
    """

    produced: bool = False
    answered: bool = False
    failure: Exception | None = None


def install_model_media_guard(model: Model) -> None:
    """Retry replayed and tool-produced media failures at the final async model boundary.

    The wrappers are ``partial`` objects over the bound originals rather than
    closures: Agno deep-copies models routinely (``Model.__deepcopy__``), and
    ``deepcopy`` rebinds a partial's arguments through the memo while a closure
    would stay bound to the instance it was installed on. The guarantee is only
    as good as the chain underneath it — whatever ``model.ainvoke`` already is
    gets captured here — so the hook ``model_loading`` installs first
    (``llm_request_logging``) uses the same shape.
    """
    model_dict = vars(model)
    if model_dict.get(_GUARD_INSTALLED_ATTR) is True:
        return

    model_dict["ainvoke"] = partial(_guarded_ainvoke, model, model.ainvoke)
    model_dict["ainvoke_stream"] = partial(_guarded_ainvoke_stream, model, model.ainvoke_stream)
    model_dict["aresponse"] = partial(_guarded_aresponse, model, model.aresponse)
    model_dict["aresponse_stream"] = partial(_guarded_aresponse_stream, model, model.aresponse_stream)
    model_dict[_GUARD_INSTALLED_ATTR] = True


def _guarded_ainvoke(
    model: Model,
    original_ainvoke: Callable[..., Coroutine[object, object, ModelResponse]],
    *args: object,
    **kwargs: object,
) -> Coroutine[object, object, ModelResponse]:
    if id(model) in _ACTIVE_GUARD_MODELS.get():
        return original_ainvoke(*args, **kwargs)
    return _invoke_with_media_guard(model, original_ainvoke, args, kwargs)


def _guarded_ainvoke_stream(
    model: Model,
    original_ainvoke_stream: Callable[..., AsyncIterator[ModelResponse]],
    *args: object,
    **kwargs: object,
) -> AsyncIterator[ModelResponse]:
    if id(model) in _ACTIVE_GUARD_MODELS.get():
        return original_ainvoke_stream(*args, **kwargs)
    return _stream_with_media_guard(model, original_ainvoke_stream, args, kwargs)


async def _guarded_aresponse(
    model: Model,
    original_aresponse: Callable[..., Coroutine[object, object, object]],
    *args: object,
    **kwargs: object,
) -> object:
    """Keep current-turn provenance bound across one complete Agno model/tool loop."""
    with _bind_outer_message_ids(model, _outer_message_ids(args, kwargs)):
        return await original_aresponse(*args, **kwargs)


def _guarded_aresponse_stream(
    model: Model,
    original_aresponse_stream: Callable[..., AsyncIterator[object]],
    *args: object,
    **kwargs: object,
) -> AsyncIterator[object]:
    outer_message_ids = _outer_message_ids(args, kwargs)
    return context_bound_async_stream(
        context_factory=partial(_bind_outer_message_ids, model, outer_message_ids),
        stream_factory=partial(original_aresponse_stream, *args, **kwargs),
    )


async def _invoke_with_media_guard(
    model: Model,
    original_ainvoke: Callable[..., Coroutine[object, object, ModelResponse]],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> ModelResponse:
    request = _guarded_request(model, args, kwargs)
    with _active_guard_scope(model):
        if request is None:
            return await original_ainvoke(*args, **kwargs)
        failure: Exception | None = None
        # `attempts()` always yields the request as sent, and a rung is only
        # popped once its predecessor left a successor behind.
        pending = request.attempts()
        while True:
            attempt = pending.pop(0)
            if failure is not None:
                _log_media_retry(request, attempt.removed_kinds, failure)
            with _media_stripped(request, attempt.removed_kinds):
                try:
                    response = await original_ainvoke(*args, **kwargs)
                except Exception as error:
                    pending = request.attempts_after(attempt, pending, error, produced=False)
                    if not pending:
                        raise
                    failure = error
                    continue
            _record_successful_attempt(request, attempt, failure, answered=_response_carries_answer(response))
            return response


def _stream_with_media_guard(
    model: Model,
    original_ainvoke_stream: Callable[..., AsyncIterator[ModelResponse]],
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> AsyncIterator[ModelResponse]:
    provider_stream = partial(original_ainvoke_stream, *args, **kwargs)
    request = _guarded_request(model, args, kwargs)
    if request is None:
        return _unguarded_stream(provider_stream)
    return _guarded_stream(request, provider_stream)


async def _unguarded_stream(
    provider_stream: Callable[[], AsyncIterator[ModelResponse]],
) -> AsyncGenerator[ModelResponse, None]:
    """Pass a request the guard owns no media in straight through, closing it on the way out."""
    stream = provider_stream()
    try:
        async for response in stream:
            yield response
    finally:
        await _close_quietly(stream)


async def _guarded_stream(
    request: _GuardedRequest,
    provider_stream: Callable[[], AsyncIterator[ModelResponse]],
) -> AsyncGenerator[ModelResponse, None]:
    """Walk the same ladder the blocking path walks, one provider stream per rung."""
    failure: Exception | None = None
    pending = request.attempts()
    while True:
        attempt = pending.pop(0)
        if failure is not None:
            _log_media_retry(request, attempt.removed_kinds, failure)
        pull = _StreamAttempt()
        # `aclosing` because a consumer that closes us mid-stream must finalize the
        # provider request underneath, not leave it to the GC hook.
        async with aclosing(_stream_attempt(request, attempt.removed_kinds, provider_stream, pull)) as chunks:
            async for response in chunks:
                yield response
        if pull.failure is None:
            # Only a stream the provider carried to its end proves anything; one
            # that dies halfway is the same failure wearing a prefix. Ending
            # without raising is not enough either: a stream whose chunks carried
            # no content and no tool call answered nothing, which is the bar the
            # drivers apply to a terminal run output.
            _record_successful_attempt(request, attempt, failure, answered=pull.answered)
            return
        pending = request.attempts_after(attempt, pending, pull.failure, produced=pull.produced)
        if not pending:
            raise pull.failure
        failure = pull.failure


async def _stream_attempt(
    request: _GuardedRequest,
    removed_kinds: frozenset[MediaKind],
    provider_stream: Callable[[], AsyncIterator[ModelResponse]],
    attempt: _StreamAttempt,
) -> AsyncGenerator[ModelResponse, None]:
    """Run one provider stream, holding the media substitution only while the provider is touched.

    The caller's list is agno's live run state: it reads that list to persist the
    run and to build the cancellation record, so the stripped copies must be out
    of it whenever control is anywhere but inside a provider pull. That is the
    same open-pull-close scoping ``context_bound_async_stream`` uses.

    A provider failure is reported through ``attempt`` instead of raised: only
    the caller knows whether another experiment is left to try.
    """
    with _media_stripped(request, removed_kinds):
        stream = provider_stream()
    try:
        while True:
            try:
                with _media_stripped(request, removed_kinds):
                    response = await anext(stream)
            except StopAsyncIteration:
                return
            except Exception as error:
                attempt.failure = error
                return
            attempt.produced = True
            attempt.answered = attempt.answered or _response_carries_answer(response)
            yield response
    finally:
        with _media_stripped(request, removed_kinds):
            await _close_quietly(stream)


async def _close_quietly(stream: AsyncIterator[ModelResponse]) -> None:
    """Finalize a provider stream without masking the failure that drives the retry decision."""
    try:
        await close_async_stream(stream)
    except Exception as close_error:
        logger.debug(
            "Failed to close provider stream",
            error=redact_sensitive_text(str(close_error), max_length=_MAX_LOGGED_ERROR_CHARS),
        )


@contextmanager
def _active_guard_scope(model: Model) -> Iterator[None]:
    """Keep a model that delegates one guarded method to another from retrying twice.

    Only the blocking path binds this: ``CodexResponses.ainvoke`` consumes its
    own ``ainvoke_stream``, never the reverse, and a coroutine can set and reset
    a ContextVar around its awaits while an async generator cannot (its body
    runs in whichever context resumed it, so the reset token may not belong
    there). The streamed guard therefore only reads the scope.
    """
    token = _ACTIVE_GUARD_MODELS.set(_ACTIVE_GUARD_MODELS.get() | {id(model)})
    try:
        yield
    finally:
        _ACTIVE_GUARD_MODELS.reset(token)


@contextmanager
def _bind_outer_message_ids(model: Model, outer_message_ids: frozenset[str] | None) -> Iterator[None]:
    """Bind the messages that existed before Agno could append tool-produced media."""
    bindings = dict(_outer_message_ids_by_model())
    if outer_message_ids is None:
        # Unreadable request: fall back to history provenance alone rather than
        # inherit a binding an enclosing loop left behind for this model.
        bindings.pop(id(model), None)
    else:
        bindings[id(model)] = outer_message_ids
    token = _OUTER_MESSAGE_IDS.set(bindings)
    try:
        yield
    finally:
        _OUTER_MESSAGE_IDS.reset(token)


def _outer_message_ids_by_model() -> dict[int, frozenset[str]]:
    """Return the per-model provenance bindings active in this context."""
    return _OUTER_MESSAGE_IDS.get() or {}


def _outer_message_ids(args: tuple[object, ...], kwargs: dict[str, object]) -> frozenset[str] | None:
    """Return stable IDs for the non-history messages one model/tool loop starts with."""
    messages = _request_messages(args, kwargs)
    if messages is None:
        return None
    return frozenset(message.id for message in messages if not message.from_history)


def _guarded_request(
    model: Model,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> _GuardedRequest | None:
    """Classify one outgoing request, or return ``None`` when the guard owns no media in it."""
    messages = _request_messages(args, kwargs)
    if messages is None:
        return None

    outer_message_ids = _outer_message_ids_by_model().get(id(model))
    guard_owned_kinds: set[MediaKind] = set()
    outer_owned_kinds: set[MediaKind] = set()
    blank_turn_media_kinds: list[frozenset[MediaKind]] = []
    for message in messages:
        kinds = message_media_kinds(message)
        if not kinds:
            continue
        if _guard_owns_message(message, outer_message_ids=outer_message_ids):
            guard_owned_kinds |= kinds
            if _content_is_blank(message.content):
                blank_turn_media_kinds.append(kinds)
        else:
            outer_owned_kinds |= kinds
    if not guard_owned_kinds:
        return None

    route = build_model_media_route(model)
    if route is None:
        return None
    cached_kinds = unsupported_replayed_media_kinds_for_route(route) & guard_owned_kinds
    return _GuardedRequest(
        messages=messages,
        route=route,
        cached_kinds=cached_kinds,
        guard_owned_kinds=frozenset(guard_owned_kinds) - cached_kinds,
        outer_owns_media=bool(outer_owned_kinds),
        outer_message_ids=outer_message_ids,
        blank_turn_media_kinds=tuple(blank_turn_media_kinds),
    )


def _request_messages(args: tuple[object, ...], kwargs: dict[str, object]) -> list[Message] | None:
    """Read the outgoing message list, which Agno always passes by keyword.

    ``Model._ainvoke_with_retry`` calls ``self.ainvoke(**kwargs)``
    (``agno/models/base.py:289``), ``_ainvoke_stream_with_retry`` calls
    ``self.ainvoke_stream(**kwargs)`` (``:390``), and every ``aresponse`` caller
    in Agno passes ``messages=``. A positional list therefore means Agno changed
    its call shape and the guard is silently off, which is worth saying out loud
    rather than guessing at ``args``.
    """
    messages = kwargs.get("messages")
    if messages is None and args:
        _warn_positional_messages_disable_the_guard()
        return None
    if isinstance(messages, list) and all(isinstance(message, Message) for message in messages):
        return cast("list[Message]", messages)
    return None


@cache
def _warn_positional_messages_disable_the_guard() -> None:
    """Say once per process that Agno's call shape no longer matches the guard."""
    logger.warning(
        "Model media guard is inactive: agno passed messages positionally",
        expected="messages= keyword (agno/models/base.py:289 and :390)",
    )


def _guard_owns_message(message: Message, *, outer_message_ids: frozenset[str] | None) -> bool:
    """Report whether a message reached the wire without the outer layer putting it there."""
    return message.from_history or (outer_message_ids is not None and message.id not in outer_message_ids)


def _media_failure_is_retryable(error: Exception) -> bool:
    """Report whether the provider read this request and rejected the request itself.

    Only a client-error status is that evidence. A transient failure says
    nothing about the payload — the provider was overloaded, rate limited, or
    unreachable — and it is owned by the retry ladders that already exist for it
    (``claude_stream_retry`` outside this hook and ``Model._ainvoke_with_retry``
    outside that); dropping media in response would answer blind on a model that
    never objected to it. Agno preserves the real HTTP status for provider
    rejections (``agno/models/openai/chat.py:443``,
    ``agno/models/anthropic/claude.py:719``) and falls back to the 502 default
    for connection errors and unexpected exceptions, so anything outside the 4xx
    range is either transient or unattributed.

    401 and 403 are the client errors media cannot explain, and they are
    permanent for the whole process: a rejected key would otherwise pay the full
    ladder on every turn whose history carries media. They arrive here as plain
    ``ModelProviderError``: agno raises ``ModelAuthenticationError`` only when a
    key is *missing* at client construction (``agno/models/xai/xai.py:53``),
    while a key the provider *rejects* comes back as an ``APIStatusError``
    re-raised with its real status (``agno/models/openai/chat.py:452-469``).

    404 is not one of them. It reads like a missing model ID, but it is the
    status the failure this guard exists for actually arrives with: OpenRouter
    answers 404 with ``"No endpoints found that support image input"`` when it
    can serve the model but not the modality (``docs/images.md``). Excluding it
    would switch the guard off for its own reproduction case, and the run-input
    layer already treats that wording as a media failure
    (``tests/test_ai_user_id.py``). A genuinely wrong model ID costs one ladder
    per turn, which is the price of not matching on provider prose.

    A deterministic safeguard refusal is a decision about the content, not about
    the modality, and replaying it without media cannot change it.
    """
    if not isinstance(error, ModelProviderError) or isinstance(error, ModelRateLimitError):
        return False
    if is_model_safeguard_refusal(error):
        return False
    if error.status_code in TRANSIENT_PROVIDER_STATUS_CODES or error.status_code in _CALLER_REJECTED_STATUS_CODES:
        return False
    return error.status_code in _CLIENT_ERROR_STATUS_CODES


def _record_successful_attempt(
    request: _GuardedRequest,
    attempt: _MediaAttempt,
    error: Exception | None,
    *,
    answered: bool,
) -> None:
    """Report what the guard did to make this attempt work, and learn what it proves.

    Anything the ladder took off beyond the strip the route already applies to
    every request is a recovery the guard caused, and the layer that owns run
    input cannot see it: ``ai.py`` and ``teams.py`` would otherwise credit their
    own without-media retry for a success that came from a replayed attachment
    leaving the wire (``MediaRetryDecision.record_retry_success``). Suppressing
    that credit is the conservative direction, so it is unconditional — what the
    attempt emitted does not change the fact that the guard, not the driver,
    took the media off.

    Learning is narrower still. The request as sent (``isolated_kind`` unset) is
    not an experiment, a retry that could have passed because a payload shrank
    below a limit shows nothing about capability, and neither does one that also
    gave an empty turn its first content. ``answered`` is the wire's spelling of
    the bar every driver applies to a terminal run output: a call that came back
    without raising but carried no content and called no tool proves nothing
    about the media either. It has no default — a caller that forgets it is a
    caller that would bank by omission, which is how the blocking path banked
    from an empty completion. Those stay unlearned and the guard simply repeats
    the experiment next turn.
    """
    if attempt.removed_kinds - request.cached_kinds:
        note_wire_media_recovery()
    if attempt.isolated_kind is None or error is None:
        return
    if attempt.fills_an_empty_turn or capability_teaching_blocked(error) or not answered:
        return
    record_replayed_media_isolation(request.route, attempt.isolated_kind)


def _response_carries_answer(response: ModelResponse) -> bool:
    """Report whether one wire response carries an answer of any kind.

    The twin of ``ai._stream_produced_answer`` and of the run-output emptiness
    test in ``run_output_status``, in the only terms the wire has. Content that
    is not a string is provider-shaped structured output, which is an answer.

    A model answers through more channels than text. Gemini appends an
    ``inline_data`` part to ``images`` and never touches ``content``, and an
    OpenAI audio answer arrives on ``audio`` with ``content`` left ``None``, so
    reading text and tool calls alone would call a delivered media answer
    "answered nothing" and refuse the lesson that experiment earned. A route
    that answers in media would then re-walk the whole ladder every turn and
    never converge.
    """
    if response.tool_calls:
        return True
    if response.images or response.videos or response.audios or response.files or response.audio:
        return True
    if response.parsed is not None:
        return True
    content = response.content
    if isinstance(content, str):
        return bool(content.strip())
    return content is not None


@contextmanager
def _media_stripped(request: _GuardedRequest, removed_kinds: frozenset[MediaKind]) -> Iterator[None]:
    """Send the guard-owned messages without the given media, then hand the caller its own back."""
    substitutions = _substitute_stripped_messages(request, removed_kinds)
    try:
        yield
    finally:
        _restore_substituted_messages(request.messages, substitutions)


def _substitute_stripped_messages(
    request: _GuardedRequest,
    removed_kinds: frozenset[MediaKind],
) -> list[tuple[Message, Message]]:
    """Swap stripped copies into the caller's list in place, preserving the list itself.

    Agno mutates the very list it passes down — ``_ainvoke_with_retry`` appends
    retry guidance to it and ``_remove_temporary_messages`` rewrites it with a
    slice assignment — so handing the provider a different list would silently
    drop those edits. Copies are shallow: only the media attributes are replaced,
    never anything nested, so the caller's persisted media objects are untouched.
    """
    if not removed_kinds:
        return []
    substitutions: list[tuple[Message, Message]] = []
    for position, message in enumerate(request.messages):
        stripped_kinds = message_media_kinds(message) & removed_kinds
        if not stripped_kinds or not request.owns(message):
            continue
        replacement = message.model_copy()
        strip_media_kinds_from_message(replacement, stripped_kinds)
        replacement.content = _content_with_media_note(message.content, _omitted_media_note(stripped_kinds))
        request.messages[position] = replacement
        substitutions.append((replacement, message))
    return substitutions


def _restore_substituted_messages(
    messages: list[Message],
    substitutions: list[tuple[Message, Message]],
) -> None:
    """Put the caller's own message objects back, matched by identity, not position.

    Agno may reorder or filter the list while the request is in flight, so the
    index a copy went in at is not the index it comes back out of.
    """
    if not substitutions:
        return
    originals_by_replacement = {id(replacement): original for replacement, original in substitutions}
    for position, message in enumerate(messages):
        original = originals_by_replacement.get(id(message))
        if original is not None:
            messages[position] = original


def _omitted_media_note(kinds: frozenset[MediaKind]) -> str:
    return _OMITTED_MEDIA_NOTE_TEMPLATE.format(kinds=", ".join(sorted(kinds)))


def _content_with_media_note(content: str | list[Any] | None, note: str) -> str | list[Any]:
    """Say what was dropped on the message that lost it, and never leave it empty.

    Stripping the media off a message whose text was only a caption can leave the
    provider with an empty turn, which some reject outright.
    """
    if isinstance(content, list):
        return _content_parts_with_media_note(content, note)
    if isinstance(content, str):
        return f"{content}\n\n{note}" if content.strip() else note
    return note


def _content_is_blank(content: str | list[Any] | None) -> bool:
    """Report whether the note would become this message's entire content."""
    if isinstance(content, list):
        # A content list either has a text part to append to or takes no note at
        # all, so the note never becomes the whole message.
        return False
    return not content or not content.strip()


def _content_parts_with_media_note(parts: list[Any], note: str) -> list[Any]:
    """Append the note in whichever text-part shape this content list already uses.

    Content parts are provider-shaped and reach the API as they are: OpenAI and
    Anthropic want ``{"type": "text", "text": ...}`` while Bedrock's Converse API
    wants ``{"text": ...}`` (``agno/models/aws/bedrock.py:321`` extends the
    request with the list verbatim). Copying the shape of a text part that is
    already there is the only way to disclose the loss without inventing one the
    provider would reject; with no such part to copy, silence is the safe answer.
    """
    template = next((part for part in parts if isinstance(part, dict) and "text" in part), None)
    if template is None:
        return parts
    note_part = {"type": template["type"], "text": note} if "type" in template else {"text": note}
    return [*parts, note_part]


def _log_media_retry(request: _GuardedRequest, removed_kinds: frozenset[MediaKind], error: Exception) -> None:
    logger.warning(
        "Retrying model request without replayed or tool-produced media",
        provider=request.route.provider,
        model_id=request.route.model_id,
        removed_media_kinds=sorted(removed_kinds),
        error_type=type(error).__name__,
        status_code=error.status_code if isinstance(error, ModelProviderError) else None,
        error=redact_sensitive_text(str(error), max_length=_MAX_LOGGED_ERROR_CHARS),
    )
