# Adapted from Agno (https://github.com/agno-agi/agno), Copyright 2025-2026 Agno Inc., Apache License 2.0; modified by MindRoom.
"""Map provider failures to the normalized provider errors, which alone decide retries and the user's reply.

Every adapter hands a transport or sign-in failure to ``provider_failure`` and a failed response, an HTTP status
or an error event inside a stream, to ``classify_provider_error``, so the retry stages, the media fallback, and the
error reply see one rule with one precedence.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING

import httpx

from mindroom.failure_text import failure_text
from mindroom.llm.errors import (
    MISSING_API_KEY,
    QUOTA_EXHAUSTED,
    TOKENS_PER_MINUTE,
    TRANSPORT_TIMEOUT,
    AuthenticationFailed,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

__all__ = [
    "CliLoginError",
    "classify_provider_error",
    "missing_api_key_error",
    "provider_failure",
    "retry_after_seconds",
]

_RATE_LIMIT_STATUS = 429
_UNAUTHORIZED_STATUS = 401
# Statuses after which an unchanged request can succeed, the ones the OpenAI and Claude SDKs retried: a request
# timeout, OpenAI's transient conflict (409), and every server error, among them Anthropic's overloaded model (529),
# which the error message reports as temporary unavailability rather than a rate limit, and a CDN's origin failures
# (520 to 524) in front of a custom endpoint.
_TRANSIENT_CLIENT_STATUSES = frozenset({408, 409})
_SERVER_ERROR_RANGE = range(500, 600)
_INVALID_REQUEST_STATUSES = frozenset({400, 404, 413, 422})
_HTTP_CLIENT_ERROR_RANGE = range(400, 500)
# The names providers give a throttled request: Anthropic's error type and the Responses code.
_RATE_LIMIT_NAMES = frozenset({"rate_limit_error", "rate_limit_exceeded"})
# The names of a 429 for an account that only a change to its billing or limits lets through. OpenAI's codes: prepaid
# credit used up, a reached organization or project spend limit, and a reached OpenAI-assigned usage limit, besides
# ``insufficient_quota``, the older code of an account without credit and still the error type of the others.
# Moonshot's error type, which it sends without a code, for an account whose balance or token quota ran out or that was
# disabled.
_QUOTA_EXHAUSTED_NAMES = frozenset(
    {
        "insufficient_quota",
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "exceeded_current_quota_error",
    },
)
# The name Anthropic gives rejected credentials, with or without its 401 status.
_AUTHENTICATION_ERROR_TYPE = "authentication_error"
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
# Anthropic's 403 permission error that names the API key, and a 403 "not authorized": an API key, or a word of the
# auth family in either spelling. Each is a whole word of the lowercased text, one not inside a longer run of letters or
# digits, so ``invalid_auth`` holds ``auth`` while an ``author`` property, an ``authority``, or ``oauth`` holds none.
_CREDENTIAL_WORDING = re.compile(
    r"(?<![a-z0-9])(?:(?:un)?auth(?:enticat[a-z]*|ori[sz][a-z]*)?|api[ _]?keys?)(?![a-z0-9])",
)

# How OpenAI words the 429 of a request larger than the account's tokens-per-minute limit, which it sends with the
# ``rate_limit_exceeded`` code: "Request too large for gpt-4o in organization org-... on tokens per min (TPM): Limit
# 30000, Requested 50000." Waiting never lets such a request through; only a smaller one passes.
_REQUEST_TOO_LARGE = "request too large"

# Wording providers use when a request does not fit the model's context window, such as the payload-size wording of
# OpenAI-compatible servers and Anthropic, older Claude models' "input length and `max_tokens` exceed context limit",
# and llama.cpp's "the request exceeds the available context size". A bare ``max_tokens`` is too broad to count.
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
    _REQUEST_TOO_LARGE,
    "input too long",
    "prompt is too long",
    "prompt too long",
    "exceeds the model",
    "available context size",
)


class CliLoginError(ValueError):
    """A CLI's saved OAuth login, such as Codex's or Kimi Code's, could not lend a usable token.

    ``status_code`` is the HTTP status of a token refresh the login server answered with an error.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _is_context_window_error(lowered: str, *, code: str | None) -> bool:
    """Return whether an error's code or lowercased wording says the request does not fit the context window."""
    return code == "context_length_exceeded" or any(marker in lowered for marker in _CONTEXT_WINDOW_MARKERS)


def classify_provider_error(  # noqa: PLR0911 - one return per rule
    message: str,
    *,
    provider: str,
    status_code: int | None = None,
    code: str | None = None,
    error_type: str | None = None,
    retry_after_seconds: float | None = None,
) -> ProviderError:
    """Return the normalized error of one failed provider response; the first rule that matches decides.

    ``status_code`` is the HTTP status. An error event inside a stream that already succeeded has none, and a
    numeric ``code``, as OpenAI-compatible servers send in one, counts as its status. ``code`` and ``error_type``
    name the failure as the provider does, such as ``rate_limit_error`` or ``server_error``; the error keeps ``code``
    as its ``provider_code``. ``retry_after_seconds`` is the response's ``Retry-After`` hint.

    1. A 429 status or a rate-limit name is ``RateLimited``, unless its wording says the request alone is larger than
       the account's tokens-per-minute limit, which makes it ``ContextWindowExceeded`` marked ``TOKENS_PER_MINUTE``:
       only a smaller request passes.
    2. A transient status, 408, 409, or any 5xx, is ``ProviderUnavailable``.
    3. A 401 status or Anthropic's ``authentication_error`` is ``AuthenticationFailed``.
    4. Wording or a code that names the context window, on a client error or an event without a status, is
       ``ContextWindowExceeded``: a retry cannot make the request fit. A 5xx that mentions it stays transient.
    5. A transient name, or Anthropic's overloaded wording, is ``ProviderUnavailable``.
    6. Wording that names the credentials, on a client error or an event without a status, is
       ``AuthenticationFailed``.
    7. An invalid-request status is ``InvalidRequest``.
    8. Anything else, such as a 403 or an unknown status, is a final ``ProviderError``.

    A name that says the account has no credit left or reached its monthly spend or usage limit outranks every rule:
    whatever its status, it is a final ``ProviderError`` marked ``QUOTA_EXHAUSTED``, since no wait lets it through.
    """
    status = status_code if status_code is not None else (int(code) if code is not None and code.isdecimal() else None)
    names = {code, error_type}
    lowered = message.casefold()
    client_side = status is None or status in _HTTP_CLIENT_ERROR_RANGE

    def typed(kind: type[ProviderError], *, marker: str | None = None) -> ProviderError:
        return kind(
            message,
            provider=provider,
            status_code=status_code,
            code=marker,
            provider_code=code,
            retry_after_seconds=retry_after_seconds,
        )

    if names & _QUOTA_EXHAUSTED_NAMES:
        return typed(ProviderError, marker=QUOTA_EXHAUSTED)
    if status == _RATE_LIMIT_STATUS or names & _RATE_LIMIT_NAMES:
        too_large = _REQUEST_TOO_LARGE in lowered
        return typed(ContextWindowExceeded, marker=TOKENS_PER_MINUTE) if too_large else typed(RateLimited)
    if status in _TRANSIENT_CLIENT_STATUSES or status in _SERVER_ERROR_RANGE:
        return typed(ProviderUnavailable)
    if status == _UNAUTHORIZED_STATUS or _AUTHENTICATION_ERROR_TYPE in names:
        return typed(AuthenticationFailed)
    if client_side and _is_context_window_error(lowered, code=code):
        return typed(ContextWindowExceeded)
    if names & _TRANSIENT_NAMES or _OVERLOADED_ERROR_TYPE in lowered:
        return typed(ProviderUnavailable)
    if client_side and _CREDENTIAL_WORDING.search(lowered):
        return typed(AuthenticationFailed)
    if status in _INVALID_REQUEST_STATUSES:
        return typed(InvalidRequest)
    return typed(ProviderError)


def retry_after_seconds(headers: Mapping[str, str], *, now: datetime | None = None) -> float | None:
    """Return a response's ``Retry-After`` hint in seconds, read as the SDKs read it.

    Azure's ``retry-after-ms`` wins, then a number of seconds in ``Retry-After``, then an HTTP date there, counted from
    ``now``. A negative number is no hint, and a date already past is no wait. Header names match in any case.
    """
    by_name = {name.casefold(): value for name, value in headers.items()}
    for name, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        raw_value = by_name.get(name)
        if raw_value is None:
            continue
        try:
            seconds = float(raw_value.strip()) / divisor
        except ValueError:
            continue
        return seconds if seconds >= 0 else None
    return _seconds_until(by_name.get("retry-after"), now=now)


def _seconds_until(http_date: str | None, *, now: datetime | None) -> float | None:
    """Return the seconds from ``now`` until an HTTP date, zero for one already past, or None for no date."""
    if http_date is None:
        return None
    try:
        when = parsedate_to_datetime(http_date)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max((when - (now or datetime.now(UTC))).total_seconds(), 0.0)


def missing_api_key_error(env_var: str, *, provider: str) -> AuthenticationFailed:
    """Return the error for a route that has no API key, which the reply turns into how to connect one."""
    message = f"{env_var} not set. Please set the {env_var} environment variable."
    return AuthenticationFailed(message, provider=provider, status_code=401, code=MISSING_API_KEY)


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
    import httpx2  # noqa: PLC0415 - the OpenAI and Claude SDKs' transport, loaded only once a request fails
    from google.auth import exceptions as google_auth  # noqa: PLC0415 - loaded only once a request fails

    chain = list(_visible_chain(error))
    transport = (
        httpx.TransportError,
        httpx2.TransportError,
        ConnectionError,
        TimeoutError,
        aiohttp.ClientError,
        google_auth.TransportError,
    )
    if any(
        isinstance(cause, transport) or (isinstance(cause, google_auth.RefreshError) and cause.retryable)
        for cause in chain
    ):
        # A timeout as either HTTP client raises it, or as aiohttp and the socket layer raise it: the OpenAI and
        # Claude SDKs send through httpx2, the Gemini and Ollama SDKs and the CLI login refreshes through httpx.
        timeouts = (httpx.TimeoutException, httpx2.TimeoutException, TimeoutError)
        timed_out = any(isinstance(cause, timeouts) for cause in chain)
        code = TRANSPORT_TIMEOUT if timed_out else "connection_error"
        return ProviderUnavailable(failure_text(error), provider=provider, code=code)
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
