# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Map provider failures to the normalized provider errors, which alone decide retries and the user's reply.

Every adapter hands a transport or sign-in failure to ``provider_failure`` and a failed response, an HTTP status
or an error event inside a stream, to ``classify_provider_error``, so the retry stages, the media fallback, and the
error reply see one rule with one precedence.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import httpx

from mindroom.llm.errors import (
    TRANSPORT_TIMEOUT,
    AuthenticationFailed,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "CliLoginError",
    "classify_provider_error",
    "is_context_window_error",
    "provider_failure",
]

_RATE_LIMIT_STATUS = 429
_UNAUTHORIZED_STATUS = 401
# Statuses after which an unchanged request can succeed: 409 is OpenAI's transient conflict, and 529 is
# Anthropic's overloaded model, which the error message reports as temporary unavailability, not as a rate limit.
_TRANSIENT_STATUSES = frozenset({408, 409, 500, 502, 503, 504, 529})
_INVALID_REQUEST_STATUSES = frozenset({400, 404, 413, 422})
_HTTP_CLIENT_ERROR_RANGE = range(400, 500)
# The names providers give a throttled request: Anthropic's error type and the Responses code.
_RATE_LIMIT_NAMES = frozenset({"rate_limit_error", "rate_limit_exceeded"})
# Anthropic reports an overloaded model with this error type, with or without a 529 status.
_OVERLOADED_ERROR_TYPE = "overloaded_error"
# The names providers give a transient failure: Anthropic's error types, where ``timeout_error`` is a gateway
# timeout, the types OpenAI-compatible servers send, and the Responses codes.
_TRANSIENT_NAMES = frozenset(
    {
        "overloaded",
        _OVERLOADED_ERROR_TYPE,
        "server_error",
        "internal_server_error",
        "api_error",
        "timeout_error",
        "vector_store_timeout",
    },
)
# Wording of a rejection that names the credentials without a 401 status, such as Gemini's 400 "API key not valid",
# Anthropic's 403 permission error that names the API key, and a 403 "not authorized": an API key, or a whole word of
# the auth family, so text that names an ``author`` property, an ``authority``, or ``oauth`` is not one.
_CREDENTIAL_WORDING = re.compile(r"\b(?:un)?auth(?:enticat\w*|oriz\w*)?\b|api[ _]?key")

# Wording providers use when a request does not fit the model's context window (Agno's
# ``ModelProviderError.CONTEXT_WINDOW_PATTERNS`` without its over-broad ``max_tokens``, plus the
# payload-size wording OpenAI-compatible servers and Anthropic use, older Claude models' "input length and
# `max_tokens` exceed context limit", and llama.cpp's "exceeds the available context size").
_CONTEXT_WINDOW_MARKERS = (
    "context_length_exceeded",
    "context window",
    "context length",
    "context limit",
    "maximum context length",
    "maximum number of tokens",
    "token limit",
    "too many tokens",
    "payload too large",
    "content_too_large",
    "request too large",
    "input too long",
    "prompt is too long",
    "prompt too long",
    "exceeds the model",
    "context size",
)
# A timeout as the HTTP client raises it, or as aiohttp and the socket layer raise it.
_TRANSPORT_TIMEOUTS = (httpx.TimeoutException, TimeoutError)


class CliLoginError(ValueError):
    """A CLI's saved OAuth login, such as Codex's or Kimi Code's, could not lend a usable token.

    ``status_code`` is the HTTP status of a token refresh the login server answered with an error.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def is_context_window_error(message: str, *, code: str | None = None) -> bool:
    """Return whether an error's code or wording says the request does not fit the context window."""
    if code == "context_length_exceeded":
        return True
    lowered = message.casefold()
    return any(marker in lowered for marker in _CONTEXT_WINDOW_MARKERS)


def classify_provider_error(  # noqa: PLR0911 - one return per rule
    message: str,
    *,
    provider: str,
    status_code: int | None = None,
    code: str | None = None,
    error_type: str | None = None,
) -> ProviderError:
    """Return the normalized error of one failed provider response; the first rule that matches decides.

    ``status_code`` is the HTTP status. An error event inside a stream that already succeeded has none, and a
    numeric ``code``, as OpenAI-compatible servers send in one, counts as its status. ``code`` and ``error_type``
    name the failure as the provider does, such as ``rate_limit_error`` or ``server_error``.

    1. A 429 status or a rate-limit name is ``RateLimited``.
    2. A transient status is ``ProviderUnavailable``.
    3. A 401 status is ``AuthenticationFailed``.
    4. Wording or a code that names the context window, on a client error or an event without a status, is
       ``ContextWindowExceeded``: a retry cannot make the request fit. A 5xx that mentions it stays transient.
    5. A transient name, or Anthropic's overloaded wording, is ``ProviderUnavailable``.
    6. Wording that names the credentials, on a client error or an event without a status, is
       ``AuthenticationFailed``.
    7. An invalid-request status is ``InvalidRequest``.
    8. Anything else, such as a 403 or an unknown status, is a final ``ProviderError``.
    """
    status = status_code if status_code is not None else (int(code) if code is not None and code.isdecimal() else None)
    names = {code, error_type}
    lowered = message.casefold()
    client_side = status is None or status in _HTTP_CLIENT_ERROR_RANGE

    def typed(kind: type[ProviderError]) -> ProviderError:
        return kind(message, provider=provider, status_code=status_code, code=code)

    if status == _RATE_LIMIT_STATUS or names & _RATE_LIMIT_NAMES:
        return typed(RateLimited)
    if status in _TRANSIENT_STATUSES:
        return typed(ProviderUnavailable)
    if status == _UNAUTHORIZED_STATUS:
        return typed(AuthenticationFailed)
    if client_side and is_context_window_error(message, code=code):
        return typed(ContextWindowExceeded)
    if names & _TRANSIENT_NAMES or _OVERLOADED_ERROR_TYPE in lowered:
        return typed(ProviderUnavailable)
    if client_side and _CREDENTIAL_WORDING.search(lowered):
        return typed(AuthenticationFailed)
    if status in _INVALID_REQUEST_STATUSES:
        return typed(InvalidRequest)
    return typed(ProviderError)


def provider_failure(error: BaseException, *, provider: str) -> ProviderError | None:
    """Return the normalized error a transport or sign-in failure is, or None for the adapter's own mapping.

    The error and every cause it shows count, so a network failure that an SDK, the Google auth library, or a CLI
    login refresh wrapped in an error of its own is still the retryable failure it is: a refused or reset
    connection, a timeout, an HTTP client or aiohttp transport error, or a Google token endpoint that could not be
    reached or answered with a retryable error. A CLI login server that answered its token refresh with a status
    ``classify_provider_error`` calls retryable, such as a 503 or a 429, is that failure. Google credentials the
    token endpoint rejected, such as a deleted service-account key, are an authentication failure; credentials that
    cannot be found or loaded stay a configuration error, and any other CLI login failure is the adapter's to map.
    """
    import aiohttp  # noqa: PLC0415 - google-genai's transport, loaded only once a request fails
    from google.auth import exceptions as google_auth  # noqa: PLC0415 - loaded only once a request fails

    chain = list(_visible_chain(error))
    transport = (httpx.TransportError, ConnectionError, TimeoutError, aiohttp.ClientError, google_auth.TransportError)
    if any(
        isinstance(cause, transport) or (isinstance(cause, google_auth.RefreshError) and cause.retryable)
        for cause in chain
    ):
        timed_out = any(isinstance(cause, _TRANSPORT_TIMEOUTS) for cause in chain)
        code = TRANSPORT_TIMEOUT if timed_out else "connection_error"
        return ProviderUnavailable(str(error) or type(error).__name__, provider=provider, code=code)
    if any(isinstance(cause, google_auth.RefreshError) for cause in chain):
        return AuthenticationFailed(f"Google credentials were rejected: {error}", provider=provider, status_code=401)
    for cause in chain:
        if isinstance(cause, CliLoginError) and cause.status_code is not None:
            failure = classify_provider_error(str(cause), provider=provider, status_code=cause.status_code)
            return failure if failure.retryable else None
    return None


def _visible_chain(error: BaseException) -> Iterator[BaseException]:
    """Yield the error and each cause it shows, explicit or implicit, once; a suppressed context ends the chain."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
