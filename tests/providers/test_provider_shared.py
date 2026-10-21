"""The error classification and ``extra_kwargs`` parsing every provider family shares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from mindroom.llm.errors import (
    TRANSPORT_TIMEOUT,
    ContextWindowExceeded,
    InvalidRequest,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.providers.errors import classify_provider_error, is_context_window_error, transport_error
from mindroom.providers.extra_kwargs import settings_from_extra_kwargs


@pytest.mark.parametrize(
    ("status", "message", "code", "expected", "retryable"),
    [
        (429, "slow down", None, RateLimited, True),
        (529, "Overloaded", None, RateLimited, True),
        (503, "overloaded_error: the model is busy", None, RateLimited, True),
        (400, "The input token count exceeds the maximum number of tokens allowed", None, ContextWindowExceeded, False),
        (400, "too long", "context_length_exceeded", ContextWindowExceeded, False),
        (None, "prompt is too long", None, ContextWindowExceeded, False),
        (500, "context window service unavailable", None, ProviderUnavailable, True),
        (503, "The model is overloaded", None, ProviderUnavailable, True),
        (409, "conflict", None, ProviderUnavailable, True),
        (400, "Invalid JSON payload received.", None, InvalidRequest, False),
        (404, "model not found", None, InvalidRequest, False),
        (413, "payload", None, InvalidRequest, False),
        (401, "invalid api key", None, ProviderError, False),
        (403, "Permission denied", None, ProviderError, False),
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


def test_context_window_detection_reads_the_code_and_the_wording() -> None:
    """The shared markers cover the wording every provider uses for an input that does not fit."""
    assert is_context_window_error("x", code="context_length_exceeded")
    assert is_context_window_error("Request too large for gpt-6-astra")
    assert is_context_window_error("This request would exceed the context window")
    assert not is_context_window_error("max_tokens must be positive")


def test_transport_failures_are_retryable_connection_errors() -> None:
    """A failure before any response is retryable and names its cause."""
    error = transport_error(httpx.ConnectError("connection refused"), provider="Test")

    assert type(error) is ProviderUnavailable
    assert (error.retryable, error.status_code, error.code, str(error)) == (
        True,
        None,
        "connection_error",
        "connection refused",
    )
    assert str(transport_error(httpx.ReadError(""), provider="Test")) == "ReadError"


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
    error = transport_error(failure, provider="Test")

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
