"""Tests for error handling module."""

from pathlib import Path

import httpx
import pytest
from anthropic import AuthenticationError as AnthropicAuthError
from openai import AuthenticationError as OpenAIAuthError
from openai import OpenAIError

from mindroom import constants
from mindroom.error_handling import (
    MODEL_SAFEGUARD_REFUSAL_MESSAGE,
    _extract_provider_from_error,
    get_user_friendly_error_message,
    is_model_safeguard_refusal,
    minimal_mode_failure_message,
)
from mindroom.llm.errors import InvalidRequest, ModelSafeguardRefusal, ProviderError, ProviderUnavailable, RateLimited
from mindroom.providers.openai.errors import missing_api_key_error

_MOCK_RESPONSE = httpx.Response(status_code=401, request=httpx.Request("POST", "https://api.example.com"))


def test_api_key_error() -> None:
    """Test API key error message includes the original error."""
    error = Exception("Invalid API key")
    message = get_user_friendly_error_message(error, "assistant")
    assert "[assistant]" in message
    assert "Authentication failed" in message
    assert "Invalid API key" in message


def test_api_key_error_with_provider() -> None:
    """Test that provider is extracted from exception module."""
    error = OpenAIAuthError(message="Incorrect API key provided", response=_MOCK_RESPONSE, body=None)
    message = get_user_friendly_error_message(error, "assistant")
    assert "(openai)" in message
    assert "Authentication failed" in message


def test_provider_auth_error_redacts_secret_from_user_message() -> None:
    """Provider exception text should be redacted before Matrix-visible user output."""
    error = OpenAIAuthError(
        message="Incorrect API key provided: sk-test-secret",
        response=_MOCK_RESPONSE,
        body=None,
    )

    message = get_user_friendly_error_message(error, "assistant")

    assert "Authentication failed" in message
    assert "***redacted***" in message
    assert "sk-test-secret" not in message


def test_401_error() -> None:
    """Test that 401 errors are recognized as auth failures."""
    error = Exception("Error code: 401 - Unauthorized")
    message = get_user_friendly_error_message(error)
    assert "Authentication failed" in message


def test_generic_api_word_not_false_positive() -> None:
    """Test that the word 'api' alone does not trigger auth error."""
    error = Exception("Failed to connect to api endpoint")
    message = get_user_friendly_error_message(error)
    # Should NOT be auth error - just contains 'api' but no auth keywords
    assert "Authentication failed" not in message
    assert "Error:" in message


def test_model_safeguard_refusal_gives_actionable_guidance() -> None:
    """An explicit safeguard stop should not look like an empty model response."""
    error = ModelSafeguardRefusal(provider="Anthropic")
    assert is_model_safeguard_refusal(error)

    message = get_user_friendly_error_message(error, "mind")

    assert message == (
        "[mind] ⚠️ This model's safeguards blocked the request. "
        "Choose a different model (`!model list`) or revise the prompt, then try again."
    )
    assert "stop_reason" not in message


def test_stringified_model_safeguard_refusal_gives_actionable_guidance() -> None:
    """An errored run keeps a provider failure as its text, which still gets the refusal guidance."""
    message = get_user_friendly_error_message(Exception(MODEL_SAFEGUARD_REFUSAL_MESSAGE), "mind")

    assert message == (
        "[mind] ⚠️ This model's safeguards blocked the request. "
        "Choose a different model (`!model list`) or revise the prompt, then try again."
    )
    assert "stop_reason" not in message


def test_rate_limit_error() -> None:
    """Test rate limit error message."""
    error = Exception("Rate limit exceeded")
    message = get_user_friendly_error_message(error)
    assert "Rate limited" in message


def test_typed_rate_limit_error_uses_status_code() -> None:
    """Typed 429 errors remain rate limits even when their message is generic."""
    error = ProviderError("upstream unavailable", provider="OpenAI", status_code=429)

    message = get_user_friendly_error_message(error)

    assert "Rate limited" in message
    assert "temporarily unavailable" not in message


_PROVIDER_UNAVAILABLE_MESSAGE = (
    "⚠️ Model provider temporarily unavailable after automatic retries. Please try again shortly."
)
_RATE_LIMITED_MESSAGE = "⏱️ Rate limited. Please wait a moment and try again."
# How the requests transport starts its report when the Google sign-in host cannot be reached.
_OAUTH_HOST_UNREACHABLE = (
    "HTTPSConnectionPool(host='oauth2.googleapis.com', port=443): Max retries exceeded with url: /token"
)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ProviderUnavailable(_OAUTH_HOST_UNREACHABLE, provider="VertexAI", code="connection_error"),
            _PROVIDER_UNAVAILABLE_MESSAGE,
            id="unavailable-host-name-holds-auth",
        ),
        pytest.param(
            ProviderUnavailable("upstream replied 401 from the gateway", provider="OpenAI", status_code=503),
            _PROVIDER_UNAVAILABLE_MESSAGE,
            id="unavailable-wording-holds-401",
        ),
        pytest.param(
            ProviderUnavailable("failed to generate a response", provider="OpenAI", status_code=500),
            _PROVIDER_UNAVAILABLE_MESSAGE,
            id="unavailable-wording-holds-rate",
        ),
        pytest.param(
            RateLimited("oauth2 gateway throttled the request", provider="OpenAI", status_code=429),
            _RATE_LIMITED_MESSAGE,
            id="rate-limited-wording-holds-auth",
        ),
        pytest.param(
            ProviderError(
                "Project proj_1 does not have access to model gpt-6-astra",
                provider="OpenAI",
                status_code=403,
            ),
            "⚠️ Error: Project proj_1 does not have access to model gpt-6-astra",
            id="typed-403-keeps-keywords",
        ),
        pytest.param(
            ProviderError("Credentials were rejected", provider="VertexAI", status_code=401),
            "❌ Authentication failed (VertexAI): Credentials were rejected",
            id="typed-401",
        ),
        pytest.param(
            InvalidRequest("API key not valid. Please pass a valid API key.", provider="Gemini", status_code=400),
            "❌ Authentication failed (Gemini): API key not valid. Please pass a valid API key.",
            id="typed-400-keeps-keywords",
        ),
        pytest.param(
            Exception("401 Unauthorized"),
            "❌ Authentication failed: 401 Unauthorized",
            id="untyped-keeps-keywords",
        ),
    ],
)
def test_typed_provider_errors_are_decided_by_type_before_keywords(error: Exception, expected: str) -> None:
    """A typed transient, rate-limit, or 401 error picks its message by type; other errors, a 403 included, match keywords."""
    assert get_user_friendly_error_message(error) == expected


def test_overloaded_provider_error_is_user_friendly() -> None:
    """An exhausted provider overload must not dump its raw payload to Matrix."""
    error = Exception(
        "{'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}, 'request_id': 'req_secret'}",
    )

    message = get_user_friendly_error_message(error, "assistant")

    assert message == (
        "[assistant] ⚠️ Model provider temporarily unavailable after automatic retries. Please try again shortly."
    )
    assert "req_secret" not in message


def test_internal_provider_error_is_user_friendly() -> None:
    """A transient provider api_error gets the same bounded-retry message."""
    error = Exception(
        "{'type': 'error', 'error': {'type': 'api_error', 'message': 'Internal server error'}, "
        "'request_id': 'req_secret'}",
    )

    message = get_user_friendly_error_message(error)

    assert "Model provider temporarily unavailable after automatic retries" in message
    assert "req_secret" not in message


def test_json_provider_error_is_case_insensitive() -> None:
    """Structured JSON provider payloads normalize error type and message casing."""
    error = Exception(
        '{"type":"error","error":{"type":"API_ERROR","message":"Internal Server Error"},"request_id":"req_secret"}',
    )

    message = get_user_friendly_error_message(error)

    assert "Model provider temporarily unavailable after automatic retries" in message
    assert "req_secret" not in message


def test_typed_model_provider_error_is_user_friendly() -> None:
    """Typed provider errors use their status instead of message substrings."""
    error = ProviderError("upstream unavailable", provider="OpenAI", status_code=503)

    message = get_user_friendly_error_message(error)

    assert "Model provider temporarily unavailable after automatic retries" in message


def test_unstructured_overloaded_text_is_not_misclassified() -> None:
    """Arbitrary application errors containing overloaded retain useful details."""
    error = Exception("Local model overloaded while loading workspace state")

    message = get_user_friendly_error_message(error)

    assert message == "⚠️ Error: Local model overloaded while loading workspace state"


def test_unstructured_api_error_text_is_not_misclassified() -> None:
    """Provider-like words alone do not suppress an arbitrary application error."""
    error = Exception("api_error: Internal Server Error while reading local cache")

    message = get_user_friendly_error_message(error)

    assert message == "⚠️ Error: api_error: Internal Server Error while reading local cache"


def test_timeout_error() -> None:
    """Test timeout error message."""
    error = TimeoutError("Request timeout")
    message = get_user_friendly_error_message(error, "bot")
    assert "[bot]" in message
    assert "timed out" in message


def test_generic_error() -> None:
    """Test generic error shows actual error message."""
    error = ValueError("Something went wrong")
    message = get_user_friendly_error_message(error)
    assert "Error: Something went wrong" in message


def test_extract_provider_from_error() -> None:
    """Test provider extraction from exception module."""
    openai_err = OpenAIAuthError(message="test", response=_MOCK_RESPONSE, body=None)
    assert _extract_provider_from_error(openai_err) == "openai"

    anthropic_err = AnthropicAuthError(message="test", response=_MOCK_RESPONSE, body=None)
    assert _extract_provider_from_error(anthropic_err) == "anthropic"

    assert _extract_provider_from_error(Exception("test")) is None


@pytest.mark.parametrize(
    "error",
    [
        missing_api_key_error("OPENROUTER_API_KEY", provider="OpenRouter"),
        OpenAIError(
            "The api_key client option must be set either by passing api_key to the client "
            "or by setting the OPENAI_API_KEY environment variable",
        ),
        OpenAIError(
            "Missing credentials. Please pass an `api_key`, `workload_identity`, `admin_api_key`, "
            "or set the `OPENAI_API_KEY` or `OPENAI_ADMIN_KEY` environment variable.",
        ),
        TypeError(
            '"Could not resolve authentication method. Expected one of api_key, auth_token, or credentials to be set."',
        ),
    ],
)
def test_missing_provider_key_points_to_dashboard_setup(error: Exception, tmp_path: Path) -> None:
    """A missing provider key tells the user where to connect one instead of showing the raw exception."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://42.mindroom.chat/"},
    )

    message = get_user_friendly_error_message(error, "mind", runtime_paths=runtime_paths)

    assert message == (
        "[mind] 🔑 No AI provider key is set up yet, so I can't reply. "
        "Open the MindRoom dashboard (https://42.mindroom.chat), choose **Connect your AI provider**, "
        "paste your key, then send your message again."
    )


def test_missing_provider_key_without_public_url_names_dashboard() -> None:
    """Without a known dashboard URL the message still points at the dashboard setup step."""
    error = missing_api_key_error("OPENROUTER_API_KEY", provider="OpenRouter")

    message = get_user_friendly_error_message(error)

    assert "Open the MindRoom dashboard, choose **Connect your AI provider**" in message
    assert "OPENROUTER_API_KEY" not in message


def test_rejected_provider_key_is_still_an_authentication_failure() -> None:
    """A configured but invalid key is not reported as a missing key."""
    error = OpenAIAuthError(message="Incorrect API key provided", response=_MOCK_RESPONSE, body=None)

    message = get_user_friendly_error_message(error, "assistant")

    assert "Authentication failed" in message
    assert "Connect your AI provider" not in message


def test_minimal_subagent_failure_suggests_a_standard_subagent() -> None:
    """A minimal child's runtime failure points the caller at a new standard subagent, not the mode command."""
    reason = "Minimal Bash requires an active managed response owner"

    assert minimal_mode_failure_message(reason, "helper", subagent=True) == (
        "Minimal Bash requires an active managed response owner. Start a new subagent without minimal."
    )
    assert minimal_mode_failure_message(reason, "helper", subagent=False) == (
        "Minimal Bash requires an active managed response owner. Return to standard mode with `!mode helper standard`."
    )
