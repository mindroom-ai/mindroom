"""The error classification and ``extra_kwargs`` parsing every provider family shares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from mindroom.llm.errors import (
    TRANSPORT_TIMEOUT,
    AuthenticationFailed,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.providers.errors import classify_provider_error, is_context_window_error, provider_failure
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs


@pytest.mark.parametrize(
    ("status", "message", "code", "expected", "retryable"),
    [
        (429, "slow down", None, RateLimited, True),
        (529, "Overloaded", None, ProviderUnavailable, True),
        (503, "overloaded_error: the model is busy", None, ProviderUnavailable, True),
        (400, "The input token count exceeds the maximum number of tokens allowed", None, ContextWindowExceeded, False),
        (400, "too long", "context_length_exceeded", ContextWindowExceeded, False),
        (None, "prompt is too long", None, ContextWindowExceeded, False),
        (500, "context window service unavailable", None, ProviderUnavailable, True),
        (503, "The model is overloaded", None, ProviderUnavailable, True),
        (409, "conflict", None, ProviderUnavailable, True),
        (400, "Invalid JSON payload received.", None, InvalidRequest, False),
        (404, "model not found", None, InvalidRequest, False),
        (413, "payload", None, InvalidRequest, False),
        (401, "invalid api key", None, AuthenticationFailed, False),
        (403, "Permission denied", None, ProviderError, False),
        (
            403,
            "Your API key does not have permission to use the specified resource.",
            None,
            AuthenticationFailed,
            False,
        ),
        (400, "API key not valid. Please pass a valid API key.", None, AuthenticationFailed, False),
        (503, "authentication service overloaded", None, ProviderUnavailable, True),
        (520, "origin error", None, ProviderError, False),
    ],
)
def test_statuses_and_wording_classify_the_same_for_every_family(
    status: int | None,
    message: str,
    code: str | None,
    expected: type[ProviderError],
    retryable: bool,
) -> None:
    """One rule decides retryability, context-window failures, and invalid requests for every adapter."""
    error = classify_provider_error(message, provider="Test", status_code=status, code=code)

    assert type(error) is expected
    assert (error.retryable, error.status_code, error.code, error.provider) == (retryable, status, code, "Test")
    assert str(error) == message


@pytest.mark.parametrize(
    ("message", "code", "error_type", "expected"),
    [
        ("Slow down", None, "rate_limit_error", RateLimited),
        ("Slow down", "rate_limit_exceeded", None, RateLimited),
        ("Rate limit exceeded: free-models-per-min", "429", None, RateLimited),
        ("Slow down", "529", None, ProviderUnavailable),
        ("An error occurred.", "server_error", None, ProviderUnavailable),
        ("Unexpected upstream failure", None, "api_error", ProviderUnavailable),
        ("Overloaded", None, "overloaded_error", ProviderUnavailable),
        # A retry cannot make the request fit, so the context window outranks a transient name, not a status.
        ("This model's maximum context length is 128000 tokens.", None, "api_error", ContextWindowExceeded),
        ("This model's maximum context length is 128000 tokens.", "503", None, ProviderUnavailable),
        (
            "{'type': 'error', 'error': {'type': 'authentication_error', 'message': 'invalid x-api-key'}}",
            None,
            "authentication_error",
            AuthenticationFailed,
        ),
        ("Bad block", None, "invalid_request_error", ProviderError),
        ("An error occurred.", "invalid_prompt", None, ProviderError),
    ],
)
def test_an_error_event_without_a_status_is_classified_by_its_name_and_wording(
    message: str,
    code: str | None,
    error_type: str | None,
    expected: type[ProviderError],
) -> None:
    """An event inside a stream that already succeeded follows the same precedence as an HTTP error."""
    error = classify_provider_error(message, provider="Test", code=code, error_type=error_type)

    assert type(error) is expected
    assert (error.status_code, error.code) == (None, code)


def test_context_window_detection_reads_the_code_and_the_wording() -> None:
    """The shared markers cover the wording every provider uses for an input that does not fit."""
    assert is_context_window_error("x", code="context_length_exceeded")
    assert is_context_window_error("Request too large for gpt-6-astra")
    assert is_context_window_error("This request would exceed the context window")
    assert not is_context_window_error("max_tokens must be positive")


def test_transport_failures_are_retryable_connection_errors() -> None:
    """A failure before any response is retryable and names its cause."""
    error = provider_failure(httpx.ConnectError("connection refused"), provider="Test")

    assert type(error) is ProviderUnavailable
    assert (error.retryable, error.status_code, error.code, str(error)) == (
        True,
        None,
        "connection_error",
        "connection refused",
    )
    read_error = provider_failure(httpx.ReadError(""), provider="Test")
    assert read_error is not None
    assert str(read_error) == "ReadError"


class _LoginRefreshError(Exception):
    """A login helper's own error, as a CLI login refresh raises it."""


def _wrapped(cause: Exception, *, suppress: bool = False) -> _LoginRefreshError:
    """Return a login helper's error raised ``from cause``, or ``from None`` with ``cause`` as its context."""
    wrapped = _LoginRefreshError(f"refresh failed: {cause}")
    if suppress:
        wrapped.__context__ = cause
        wrapped.__suppress_context__ = True
    else:
        wrapped.__cause__ = cause
    return wrapped


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (_wrapped(httpx.ConnectError("connection refused")), "connection_error"),
        (_wrapped(httpx.ReadTimeout("timed out")), TRANSPORT_TIMEOUT),
        (_wrapped(ConnectionResetError(104, "reset")), "connection_error"),
        (_wrapped(_wrapped(TimeoutError())), TRANSPORT_TIMEOUT),
    ],
    ids=["refused", "read-timeout", "reset", "nested-timeout"],
)
def test_a_transport_failure_wrapped_in_another_error_is_still_retryable(error: Exception, code: str) -> None:
    """The cause chain decides, so an SDK or login helper that wraps a network failure keeps it transient."""
    failure = provider_failure(error, provider="Test")

    assert type(failure) is ProviderUnavailable
    assert (failure.code, str(failure)) == (code, str(error))


@pytest.mark.parametrize(
    "error",
    [
        _wrapped(httpx.ConnectError("connection refused"), suppress=True),
        ValueError("invalid literal"),
        OSError("disk full"),
    ],
    ids=["suppressed-cause", "unrelated", "bare-os-error"],
)
def test_only_a_transport_failure_is_normalized_as_one(error: Exception) -> None:
    """A suppressed cause, or an error with no network failure behind it, is left to the adapter's own mapping."""
    assert provider_failure(error, provider="Test") is None


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(httpx.ReadTimeout(""), id="http-read"),
        pytest.param(httpx.PoolTimeout(""), id="http-pool"),
        pytest.param(TimeoutError(), id="socket"),
    ],
)
def test_transport_timeouts_carry_the_timeout_code(failure: Exception) -> None:
    """A timeout before any response is retryable and says it timed out by its code, whatever its text."""
    error = provider_failure(failure, provider="Test")

    assert type(error) is ProviderUnavailable
    assert (error.retryable, error.code) == (True, TRANSPORT_TIMEOUT)


@dataclass(frozen=True, slots=True, kw_only=True)
class _Settings:
    api_key: str | None = None
    timeout: float | None = None
    client_params: dict[str, Any] | None = None
    stop: list[str] | None = None


def test_unknown_extra_kwargs_fail_with_the_accepted_keys() -> None:
    """Unknown keys fail at construction and the message lists what the settings type takes."""
    with pytest.raises(
        ValueError,
        match=r"Unsupported extra_kwargs for provider 'test': retries, zzz\. Accepted keys: ",
    ) as info:
        settings_from_extra_kwargs(_Settings, {"zzz": 1, "retries": 2, "timeout": 3}, provider="test")

    assert str(info.value).endswith("Accepted keys: api_key, client_params, stop, timeout")


def test_accepted_keys_narrow_the_declared_fields_per_route() -> None:
    """A route may accept a subset of a shared settings type; keys outside it fail like undeclared ones."""
    with pytest.raises(ValueError, match="timeout") as info:
        settings_from_extra_kwargs(_Settings, {"timeout": 3}, provider="test", accepted={"api_key", "unknown"})

    assert str(info.value).endswith("Accepted keys: api_key")
    assert settings_from_extra_kwargs(_Settings, {"api_key": "k"}, provider="test", accepted={"api_key"}).api_key == "k"


def test_authored_containers_are_copied_and_opaque_values_kept() -> None:
    """Request building may mutate the parsed settings without touching the config; credentials pass through."""
    transport = httpx.MockTransport(lambda _request: httpx.Response(200))
    authored = {"client_params": {"transport": transport, "headers": {"a": "b"}}, "stop": ["x"]}

    settings = settings_from_extra_kwargs(_Settings, authored, provider="test")

    assert settings.client_params is not None
    assert settings.client_params["transport"] is transport
    assert settings.client_params["headers"] == {"a": "b"}
    assert settings.client_params["headers"] is not authored["client_params"]["headers"]
    assert settings.stop == ["x"]
    assert settings.stop is not authored["stop"]
    assert settings_from_extra_kwargs(_Settings, None, provider="test") == _Settings()
