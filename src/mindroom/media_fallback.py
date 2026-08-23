"""Retry one rejected provider request without inline media."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial
from typing import TYPE_CHECKING, cast

from agno.exceptions import ModelProviderError, ModelRateLimitError
from agno.models.message import Message

from mindroom.error_handling import TRANSIENT_PROVIDER_STATUS_CODES, is_model_safeguard_refusal
from mindroom.logging_config import get_logger
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.context_bound_streams import close_async_stream

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Iterator

    from agno.models.base import Model
    from agno.models.response import ModelResponse

__all__ = ["install_model_media_fallback"]

logger = get_logger(__name__)

_INSTALLED_ATTR = "_mindroom_model_media_fallback_installed"
_FALLBACK_MARKER = "[Inline media unavailable for this model]"
_CLIENT_ERROR_STATUS_CODES = range(400, 500)
_CALLER_REJECTED_STATUS_CODES = frozenset({401, 403})
_MAX_LOGGED_ERROR_CHARS = 500

# A blocking adapter may implement ``ainvoke`` by consuming its own
# ``ainvoke_stream`` method.
# Binding the model during the outer call keeps that delegation from installing
# a second retry owner around the same provider request.
_ACTIVE_MODELS: ContextVar[frozenset[int]] = ContextVar(
    "mindroom_active_media_fallback_models",
    default=frozenset(),
)


def install_model_media_fallback(model: Model, *, fallback_prompt: str) -> None:
    """Install one media-free retry around a model's asynchronous wire calls."""
    model_dict = vars(model)
    if model_dict.get(_INSTALLED_ATTR) is True:
        return

    model_dict["ainvoke"] = partial(_fallback_ainvoke, model, model.ainvoke, fallback_prompt)
    model_dict["ainvoke_stream"] = partial(
        _fallback_ainvoke_stream,
        model,
        model.ainvoke_stream,
        fallback_prompt,
    )
    model_dict[_INSTALLED_ATTR] = True


def _fallback_ainvoke(
    model: Model,
    original_ainvoke: Callable[..., Coroutine[object, object, ModelResponse]],
    fallback_prompt: str,
    *args: object,
    **kwargs: object,
) -> Coroutine[object, object, ModelResponse]:
    if id(model) in _ACTIVE_MODELS.get():
        return original_ainvoke(*args, **kwargs)
    return _ainvoke_with_media_fallback(model, original_ainvoke, fallback_prompt, args, kwargs)


async def _ainvoke_with_media_fallback(
    model: Model,
    original_ainvoke: Callable[..., Coroutine[object, object, ModelResponse]],
    fallback_prompt: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> ModelResponse:
    messages = _request_messages(args, kwargs)
    with _active_model(model):
        try:
            return await original_ainvoke(*args, **kwargs)
        except Exception as error:
            if messages is None or not _has_inline_media(messages) or not _is_retryable_media_failure(error):
                raise
            retry_args, retry_kwargs = _media_free_call(args, kwargs, messages, fallback_prompt)
            _log_retry(model, error)
            return await original_ainvoke(*retry_args, **retry_kwargs)


def _fallback_ainvoke_stream(
    model: Model,
    original_ainvoke_stream: Callable[..., AsyncIterator[ModelResponse]],
    fallback_prompt: str,
    *args: object,
    **kwargs: object,
) -> AsyncIterator[ModelResponse]:
    if id(model) in _ACTIVE_MODELS.get():
        return original_ainvoke_stream(*args, **kwargs)
    return _stream_with_media_fallback(model, original_ainvoke_stream, fallback_prompt, args, kwargs)


async def _stream_with_media_fallback(
    model: Model,
    original_ainvoke_stream: Callable[..., AsyncIterator[ModelResponse]],
    fallback_prompt: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> AsyncGenerator[ModelResponse, None]:
    messages = _request_messages(args, kwargs)
    with _active_model(model):
        stream = original_ainvoke_stream(*args, **kwargs)
    produced = False
    failure: Exception | None = None
    try:
        while True:
            try:
                chunk = await _next_with_active_model(model, stream)
            except StopAsyncIteration:
                return
            except Exception as error:
                if (
                    produced
                    or messages is None
                    or not _has_inline_media(messages)
                    or not _is_retryable_media_failure(error)
                ):
                    raise
                failure = error
                break
            produced = True
            yield chunk
    finally:
        await _close_quietly(model, stream)

    assert failure is not None
    retry_args, retry_kwargs = _media_free_call(args, kwargs, messages, fallback_prompt)
    _log_retry(model, failure)
    with _active_model(model):
        retry_stream = original_ainvoke_stream(*retry_args, **retry_kwargs)
    try:
        while True:
            try:
                chunk = await _next_with_active_model(model, retry_stream)
            except StopAsyncIteration:
                return
            yield chunk
    finally:
        await _close_quietly(model, retry_stream)


def _request_messages(args: tuple[object, ...], kwargs: dict[str, object]) -> list[Message] | None:
    candidate = kwargs.get("messages") if "messages" in kwargs else (args[0] if args else None)
    if isinstance(candidate, list) and all(isinstance(message, Message) for message in candidate):
        return cast("list[Message]", candidate)
    return None


def _has_inline_media(messages: list[Message]) -> bool:
    return any(message.audio or message.images or message.files or message.videos for message in messages)


def _media_free_call(
    args: tuple[object, ...],
    kwargs: dict[str, object],
    messages: list[Message],
    fallback_prompt: str,
) -> tuple[tuple[object, ...], dict[str, object]]:
    retry_messages = [_without_inline_media(message) for message in messages]
    retry_messages.append(
        Message(
            role="user",
            content=_fallback_note(fallback_prompt),
            temporary=True,
        ),
    )
    if "messages" in kwargs:
        return args, {**kwargs, "messages": retry_messages}
    return (retry_messages, *args[1:]), kwargs


def _without_inline_media(message: Message) -> Message:
    copy = message.model_copy()
    copy.audio = None
    copy.images = None
    copy.files = None
    copy.videos = None
    return copy


def _fallback_note(fallback_prompt: str) -> str:
    if _FALLBACK_MARKER in fallback_prompt:
        return fallback_prompt
    return f"{_FALLBACK_MARKER}\n{fallback_prompt}"


def _is_retryable_media_failure(error: Exception) -> bool:
    if not isinstance(error, ModelProviderError) or isinstance(error, ModelRateLimitError):
        return False
    if is_model_safeguard_refusal(error):
        return False
    if error.status_code in TRANSIENT_PROVIDER_STATUS_CODES or error.status_code in _CALLER_REJECTED_STATUS_CODES:
        return False
    return error.status_code in _CLIENT_ERROR_STATUS_CODES


@contextmanager
def _active_model(model: Model) -> Iterator[None]:
    token = _ACTIVE_MODELS.set(_ACTIVE_MODELS.get() | {id(model)})
    try:
        yield
    finally:
        _ACTIVE_MODELS.reset(token)


async def _next_with_active_model(model: Model, stream: AsyncIterator[ModelResponse]) -> ModelResponse:
    with _active_model(model):
        return await anext(stream)


async def _close_quietly(model: Model, stream: AsyncIterator[ModelResponse]) -> None:
    try:
        with _active_model(model):
            await close_async_stream(stream)
    except Exception as error:
        logger.debug(
            "Failed to close provider stream",
            error=redact_sensitive_text(str(error), max_length=_MAX_LOGGED_ERROR_CHARS),
        )


def _log_retry(model: Model, error: Exception) -> None:
    logger.warning(
        "Retrying model request without inline media",
        provider=model.provider,
        model_id=model.id,
        status_code=error.status_code if isinstance(error, ModelProviderError) else None,
        error=redact_sensitive_text(str(error), max_length=_MAX_LOGGED_ERROR_CHARS),
    )
