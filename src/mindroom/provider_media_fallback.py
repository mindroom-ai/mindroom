"""Retry one rejected provider request without inline media."""

from __future__ import annotations

from collections.abc import AsyncGenerator as AsyncGeneratorABC
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from agno.exceptions import ContextWindowExceededError, ModelProviderError, RetryableModelProviderError
from agno.models.message import Message

from mindroom.error_handling import TRANSIENT_PROVIDER_STATUS_CODES, is_model_safeguard_refusal
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Iterator, Mapping

    from agno.models.base import Model
    from agno.models.response import ModelResponse

    from mindroom.media_inputs import MediaKind

__all__ = ["install_provider_media_fallback", "reset_model_media_capability_cache"]

logger = get_logger(__name__)

_INSTALLED_ATTR = "_mindroom_provider_media_fallback_installed"
_FALLBACK_MARKER = "[Inline media unavailable for this model]"
_PAYLOAD_TOO_LARGE_STATUS = 413
_RATE_LIMIT_STATUS = 429
_SERVER_ERROR_STATUS = 500
_MAX_LOGGED_ERROR_CHARS = 500
_ACTIVE_MODELS: ContextVar[frozenset[int]] = ContextVar(
    "mindroom_active_provider_media_fallback_models",
    default=frozenset(),
)


@dataclass(frozen=True, slots=True)
class _ModelMediaRoute:
    """Concrete model route used for process-local media capability learning."""

    provider: str
    model_id: str
    base_url: str | None = None


@dataclass(slots=True)
class _MediaFallbackRequestState:
    """Media removed for the remainder of one provider retry cycle."""

    removed_kinds: frozenset[MediaKind] = frozenset()
    failure: Exception | None = None
    failed_kinds: frozenset[MediaKind] = frozenset()
    stream_output_produced: bool = False


_REQUEST_STATES: ContextVar[dict[int, _MediaFallbackRequestState] | None] = ContextVar(
    "mindroom_provider_media_fallback_request_states",
    default=None,
)


# Learned negative capabilities intentionally live only for this process lifetime.
_UNSUPPORTED_MEDIA_KINDS_BY_ROUTE: dict[_ModelMediaRoute, set[MediaKind]] = {}


@runtime_checkable
class _AsyncClosableIterator(Protocol):
    async def aclose(self) -> None: ...


def install_provider_media_fallback(model: Model, *, fallback_prompt: str) -> None:
    """Install one media-free retry around a model's asynchronous provider calls."""
    model_dict = vars(model)
    if model_dict.get(_INSTALLED_ATTR) is True:
        return

    model_dict["_ainvoke_with_retry"] = partial(
        _ainvoke_in_request_scope,
        model,
        model._ainvoke_with_retry,
    )
    model_dict["_ainvoke_stream_with_retry"] = partial(
        _ainvoke_stream_in_request_scope,
        model,
        model._ainvoke_stream_with_retry,
    )
    model_dict["_is_retryable_error"] = partial(
        _is_retryable_provider_error,
        model,
        model._is_retryable_error,
    )
    model_dict["ainvoke"] = partial(_fallback_ainvoke, model, model.ainvoke, fallback_prompt)
    model_dict["ainvoke_stream"] = partial(
        _fallback_ainvoke_stream,
        model,
        model.ainvoke_stream,
        fallback_prompt,
    )
    model_dict[_INSTALLED_ATTR] = True


async def _ainvoke_in_request_scope(
    model: Model,
    original_ainvoke_with_retry: Callable[..., Coroutine[object, object, ModelResponse]],
    *args: object,
    **kwargs: object,
) -> ModelResponse:
    with _media_fallback_request(model):
        return await original_ainvoke_with_retry(*args, **kwargs)


async def _ainvoke_stream_in_request_scope(
    model: Model,
    original_ainvoke_stream_with_retry: Callable[..., AsyncIterator[ModelResponse]],
    *args: object,
    **kwargs: object,
) -> AsyncGenerator[ModelResponse, None]:
    with _media_fallback_request(model):
        async for response in original_ainvoke_stream_with_retry(*args, **kwargs):
            yield response


def _is_retryable_provider_error(
    model: Model,
    original_is_retryable_error: Callable[[ModelProviderError], bool],
    error: ModelProviderError,
) -> bool:
    state = _request_state(model)
    return not (state is not None and state.stream_output_produced) and original_is_retryable_error(error)


def _fallback_ainvoke(
    model: Model,
    original_ainvoke: Callable[..., Coroutine[object, object, ModelResponse]],
    fallback_prompt: str,
    *args: object,
    **kwargs: object,
) -> Coroutine[object, object, ModelResponse]:
    if id(model) in _ACTIVE_MODELS.get():
        return original_ainvoke(*args, **kwargs)
    return _ainvoke_with_fallback(model, original_ainvoke, fallback_prompt, args, kwargs)


async def _ainvoke_with_fallback(
    model: Model,
    original_ainvoke: Callable[..., Coroutine[object, object, ModelResponse]],
    fallback_prompt: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> ModelResponse:
    messages = _request_messages(args, kwargs)
    route = _model_media_route(model)
    present_kinds = _media_kinds(messages)
    request_state = _request_state(model) or _MediaFallbackRequestState()
    known_unsupported = _known_unsupported_media_kinds(route) & present_kinds
    removed_kinds = known_unsupported | (request_state.removed_kinds & present_kinds)
    initial_args, initial_kwargs = _call_without_media_kinds(
        args,
        kwargs,
        messages,
        removed_kinds,
        fallback_prompt,
    )
    remaining_kinds = present_kinds - removed_kinds
    with _active_model(model):
        try:
            response = await original_ainvoke(*initial_args, **initial_kwargs)
        except Exception as error:
            if not remaining_kinds or not _should_retry(error):
                raise
            _remember_request_fallback(request_state, error, remaining_kinds)
            retry_args, retry_kwargs = _call_without_media_kinds(
                args,
                kwargs,
                messages,
                known_unsupported | request_state.removed_kinds,
                fallback_prompt,
            )
            _log_retry(model, error)
            response = await original_ainvoke(*retry_args, **retry_kwargs)
    _learn_from_request_fallback(route, request_state)
    return response


def _fallback_ainvoke_stream(
    model: Model,
    original_ainvoke_stream: Callable[..., AsyncIterator[ModelResponse]],
    fallback_prompt: str,
    *args: object,
    **kwargs: object,
) -> AsyncIterator[ModelResponse]:
    if id(model) in _ACTIVE_MODELS.get():
        return original_ainvoke_stream(*args, **kwargs)
    return _stream_with_fallback(model, original_ainvoke_stream, fallback_prompt, args, kwargs)


async def _stream_with_fallback(
    model: Model,
    original_ainvoke_stream: Callable[..., AsyncIterator[ModelResponse]],
    fallback_prompt: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> AsyncGenerator[ModelResponse, None]:
    messages = _request_messages(args, kwargs)
    route = _model_media_route(model)
    present_kinds = _media_kinds(messages)
    request_state = _request_state(model) or _MediaFallbackRequestState()
    known_unsupported = _known_unsupported_media_kinds(route) & present_kinds
    removed_kinds = known_unsupported | (request_state.removed_kinds & present_kinds)
    initial_args, initial_kwargs = _call_without_media_kinds(
        args,
        kwargs,
        messages,
        removed_kinds,
        fallback_prompt,
    )
    remaining_kinds = present_kinds - removed_kinds
    stream: AsyncIterator[ModelResponse] | None = None
    failure: Exception | None = None
    produced = False
    try:
        with _active_model(model):
            stream = original_ainvoke_stream(*initial_args, **initial_kwargs)
        while True:
            try:
                chunk = await _next_with_active_model(model, stream)
            except StopAsyncIteration:
                _learn_from_request_fallback(route, request_state)
                return
            except Exception as error:
                if produced or not remaining_kinds or not _should_retry(error):
                    raise
                failure = error
                break
            produced = True
            request_state.stream_output_produced = True
            yield chunk
    finally:
        if stream is not None:
            await _close_stream(model, stream)

    assert failure is not None
    _remember_request_fallback(request_state, failure, remaining_kinds)
    retry_args, retry_kwargs = _call_without_media_kinds(
        args,
        kwargs,
        messages,
        known_unsupported | request_state.removed_kinds,
        fallback_prompt,
    )
    _log_retry(model, failure)
    with _active_model(model):
        retry_stream = original_ainvoke_stream(*retry_args, **retry_kwargs)
    try:
        while True:
            try:
                chunk = await _next_with_active_model(model, retry_stream)
            except StopAsyncIteration:
                break
            request_state.stream_output_produced = True
            yield chunk
    finally:
        await _close_stream(model, retry_stream)
    _learn_from_request_fallback(route, request_state)


def _request_messages(args: tuple[object, ...], kwargs: dict[str, object]) -> list[Message] | None:
    candidate = kwargs.get("messages") if "messages" in kwargs else (args[0] if args else None)
    if isinstance(candidate, list) and all(isinstance(message, Message) for message in candidate):
        return cast("list[Message]", candidate)
    return None


def _media_kinds(messages: list[Message] | None) -> frozenset[MediaKind]:
    if messages is None:
        return frozenset()
    kinds: set[MediaKind] = set()
    for message in messages:
        if message.audio:
            kinds.add("audio")
        if message.images:
            kinds.add("image")
        if message.files:
            kinds.add("file")
        if message.videos:
            kinds.add("video")
    return frozenset(kinds)


def _call_without_media_kinds(
    args: tuple[object, ...],
    kwargs: dict[str, object],
    messages: list[Message] | None,
    removed_kinds: frozenset[MediaKind],
    fallback_prompt: str,
) -> tuple[tuple[object, ...], dict[str, object]]:
    if messages is None or not removed_kinds:
        return args, kwargs
    retry_messages = [_without_inline_media(message, removed_kinds) for message in messages]
    retry_messages.append(
        Message(
            role="user",
            content=f"{_FALLBACK_MARKER}\n{fallback_prompt}",
            temporary=True,
        ),
    )
    if "messages" in kwargs:
        return args, {**kwargs, "messages": retry_messages}
    return (retry_messages, *args[1:]), kwargs


def _without_inline_media(message: Message, removed_kinds: frozenset[MediaKind]) -> Message:
    copied = message.model_copy()
    if "audio" in removed_kinds:
        copied.audio = None
    if "image" in removed_kinds:
        copied.images = None
    if "file" in removed_kinds:
        copied.files = None
    if "video" in removed_kinds:
        copied.videos = None
    return copied


def _known_unsupported_media_kinds(route: _ModelMediaRoute) -> frozenset[MediaKind]:
    return frozenset(_UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.get(route, set()))


def _record_unsupported_media_kinds(
    route: _ModelMediaRoute,
    removed_kinds: frozenset[MediaKind],
) -> None:
    _UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.setdefault(route, set()).update(removed_kinds)


def _request_state(model: Model) -> _MediaFallbackRequestState | None:
    states = _REQUEST_STATES.get()
    return states.get(id(model)) if states is not None else None


def _remember_request_fallback(
    state: _MediaFallbackRequestState,
    failure: Exception,
    failed_kinds: frozenset[MediaKind],
) -> None:
    state.removed_kinds |= failed_kinds
    state.failure = failure
    state.failed_kinds = failed_kinds


def _learn_from_request_fallback(route: _ModelMediaRoute, state: _MediaFallbackRequestState) -> None:
    if state.failure is not None and _should_learn(state.failure, state.failed_kinds):
        _record_unsupported_media_kinds(route, state.failed_kinds)
    state.failure = None
    state.failed_kinds = frozenset()


def reset_model_media_capability_cache() -> None:
    """Clear learned unsupported media kinds, primarily for isolated tests."""
    _UNSUPPORTED_MEDIA_KINDS_BY_ROUTE.clear()


def _model_media_route(model: Model) -> _ModelMediaRoute:
    provider = _route_text(model.provider) or model.__class__.__name__
    model_id = _route_text(model.id) or model.__class__.__name__
    return _ModelMediaRoute(
        provider=provider.lower(),
        model_id=model_id,
        base_url=_route_endpoint(model),
    )


# Endpoint shape, rather than provider class imports, keeps optional SDKs out of
# this module's import graph. Providers expose one of these endpoint layouts.
@runtime_checkable
class _HasAzureEndpoint(Protocol):
    azure_endpoint: str | None
    base_url: object
    client_params: Mapping[str, object] | None


@runtime_checkable
class _HasHost(Protocol):
    host: str | None
    client_params: Mapping[str, object] | None


@runtime_checkable
class _HasBaseUrl(Protocol):
    base_url: object
    client_params: Mapping[str, object] | None


@runtime_checkable
class _HasClientParams(Protocol):
    client_params: Mapping[str, object] | None


def _route_endpoint(model: Model) -> str | None:
    if isinstance(model, _HasAzureEndpoint):
        return _route_endpoint_text(
            model.azure_endpoint,
            str(model.base_url) if model.base_url is not None else None,
            _client_params_endpoint(model.client_params),
        )
    if isinstance(model, _HasHost):
        return _route_endpoint_text(
            model.host,
            _client_params_endpoint(model.client_params),
        )
    if isinstance(model, _HasBaseUrl):
        return _route_endpoint_text(
            str(model.base_url) if model.base_url is not None else None,
            _client_params_endpoint(model.client_params),
        )
    if isinstance(model, _HasClientParams):
        return _client_params_endpoint(model.client_params)
    return None


def _client_params_endpoint(client_params: Mapping[str, object] | None) -> str | None:
    if client_params is None:
        return None
    for field_name in ("base_url", "host", "azure_endpoint"):
        candidate = client_params.get(field_name)
        endpoint = _route_text(candidate) if isinstance(candidate, str) else None
        if endpoint:
            return endpoint.rstrip("/")
    return None


def _route_endpoint_text(*values: str | None) -> str | None:
    for value in values:
        endpoint = _route_text(value)
        if endpoint:
            return endpoint.rstrip("/")
    return None


def _route_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _should_retry(error: Exception) -> bool:
    return not isinstance(error, RetryableModelProviderError) and not is_model_safeguard_refusal(error)


def _should_learn(error: Exception, media_kinds: frozenset[MediaKind]) -> bool:
    """Return whether stripped success isolates one unsupported media kind."""
    if len(media_kinds) != 1:
        return False
    if isinstance(error, ContextWindowExceededError):
        return False
    if isinstance(error, ModelProviderError) and (
        error.status_code in (_PAYLOAD_TOO_LARGE_STATUS, _RATE_LIMIT_STATUS)
        or error.status_code in TRANSIENT_PROVIDER_STATUS_CODES
        or error.status_code >= _SERVER_ERROR_STATUS
    ):
        return False
    lowered_error_text = str(error).lower()
    if (
        f"error code: {_PAYLOAD_TOO_LARGE_STATUS}" in lowered_error_text
        or "request entity too large" in lowered_error_text
    ):
        return False
    return not any(marker in lowered_error_text for marker in ModelProviderError.CONTEXT_WINDOW_PATTERNS)


@contextmanager
def _active_model(model: Model) -> Iterator[None]:
    token = _ACTIVE_MODELS.set(_ACTIVE_MODELS.get() | {id(model)})
    try:
        yield
    finally:
        _ACTIVE_MODELS.reset(token)


@contextmanager
def _media_fallback_request(model: Model) -> Iterator[None]:
    states = _REQUEST_STATES.get() or {}
    if id(model) in states:
        yield
        return
    token = _REQUEST_STATES.set({**states, id(model): _MediaFallbackRequestState()})
    try:
        yield
    finally:
        _REQUEST_STATES.reset(token)


async def _next_with_active_model(model: Model, stream: AsyncIterator[ModelResponse]) -> ModelResponse:
    with _active_model(model):
        return await anext(stream)


async def _close_stream(model: Model, stream: AsyncIterator[ModelResponse]) -> None:
    if isinstance(stream, (AsyncGeneratorABC, _AsyncClosableIterator)):
        with _active_model(model):
            await stream.aclose()


def _log_retry(model: Model, error: Exception) -> None:
    logger.warning(
        "Retrying model request without inline media",
        provider=model.provider,
        model_id=model.id,
        status_code=error.status_code if isinstance(error, ModelProviderError) else None,
        error=redact_sensitive_text(str(error), max_length=_MAX_LOGGED_ERROR_CHARS),
    )
