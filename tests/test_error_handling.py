"""Tests for error handling module."""

from pathlib import Path

import pytest

from mindroom import constants
from mindroom.error_handling import (
    get_user_friendly_error_message,
    is_model_safeguard_refusal,
    minimal_mode_failure_message,
)
from mindroom.llm.errors import (
    MODEL_SAFEGUARD_REFUSAL_MESSAGE,
    AuthenticationFailed,
    InvalidRequest,
    ModelSafeguardRefusal,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.usage import MessageMetrics
from mindroom.providers.errors import missing_api_key_error


@pytest.mark.parametrize(
    "text",
    [
        "Invalid API key",
        "Error code: 401 - Unauthorized",
        "Rate limit exceeded",
        "Quota exceeded for this project",
        "Request timeout while reading the workspace",
        "{'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}",
        "the api_key client option must be set",
    ],
)
def test_an_error_that_is_not_a_provider_error_shows_its_own_text(text: str) -> None:
    """Only a provider error's type reads as a provider failure; any other error's wording is shown, not classified."""
    assert get_user_friendly_error_message(RuntimeError(text), "assistant") == f"[assistant] ⚠️ Error: {text}"


def test_an_error_reply_redacts_secrets() -> None:
    """Error text is redacted before it becomes Matrix-visible reply text."""
    error = AuthenticationFailed("Incorrect API key provided: sk-test-secret", provider="OpenAI Chat", status_code=401)

    message = get_user_friendly_error_message(error, "assistant")

    assert message.startswith("[assistant] ❌ Authentication failed (OpenAI Chat): ")
    assert "***redacted***" in message
    assert "sk-test-secret" not in message


def test_model_safeguard_refusal_gives_actionable_guidance() -> None:
    """An explicit safeguard stop should not look like an empty model response."""
    error = ModelSafeguardRefusal(provider="Anthropic", usage=MessageMetrics())
    assert is_model_safeguard_refusal(error)

    message = get_user_friendly_error_message(error, "mind")

    assert message == (
        "[mind] ⚠️ This model's safeguards blocked the request. "
        "Choose a different model (`!model list`) or revise the prompt, then try again."
    )
    assert "stop_reason" not in message


def test_only_the_typed_refusal_reads_as_a_refusal() -> None:
    """An error that merely carries the refusal's text is no refusal, since every refusal reaches the reply typed."""
    error = RuntimeError(MODEL_SAFEGUARD_REFUSAL_MESSAGE)

    assert not is_model_safeguard_refusal(error)
    assert get_user_friendly_error_message(error) == f"⚠️ Error: {MODEL_SAFEGUARD_REFUSAL_MESSAGE}"


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
            RateLimited("upstream unavailable", provider="OpenAI", status_code=429),
            _RATE_LIMITED_MESSAGE,
            id="rate-limited-generic-wording",
        ),
        pytest.param(
            ProviderError(
                "Project proj_1 does not have access to model gpt-6-astra",
                provider="OpenAI",
                status_code=403,
            ),
            "⚠️ Error: Project proj_1 does not have access to model gpt-6-astra",
            id="final",
        ),
        pytest.param(
            ProviderError("upstream unavailable", provider="OpenAI", status_code=503),
            "⚠️ Error: upstream unavailable",
            id="final-status-is-not-read-again",
        ),
        pytest.param(
            AuthenticationFailed("Credentials were rejected", provider="VertexAI", status_code=401),
            "❌ Authentication failed (VertexAI): Credentials were rejected",
            id="authentication-failed",
        ),
        pytest.param(
            InvalidRequest(
                "models/gemini-x is not supported for generateContent. Call ListModels to see the api_key methods.",
                provider="Google",
                status_code=404,
            ),
            "⚠️ Error: models/gemini-x is not supported for generateContent. "
            "Call ListModels to see the api_key methods.",
            id="invalid-request-wording-is-not-read-again",
        ),
        pytest.param(
            ProviderError("Request timeout while the provider read the body", provider="Ollama"),
            "⚠️ Error: Request timeout while the provider read the body",
            id="final-timeout-wording",
        ),
    ],
)
def test_a_provider_error_is_reported_by_its_type_alone(error: Exception, expected: str) -> None:
    """A provider error's message follows its type, which its classification already took from status and wording."""
    assert get_user_friendly_error_message(error) == expected


def test_generic_error() -> None:
    """Test generic error shows actual error message."""
    error = ValueError("Something went wrong")
    message = get_user_friendly_error_message(error)
    assert "Error: Something went wrong" in message


def test_an_error_without_text_is_reported_by_its_type_name() -> None:
    """An exception without text, such as one an operation raises bare, names its type instead of nothing."""
    assert get_user_friendly_error_message(ConnectionResetError(), "bot") == "[bot] ⚠️ Error: ConnectionResetError"


@pytest.mark.parametrize("error", [TimeoutError(), TimeoutError("lock wait expired")])
def test_a_timeout_reads_as_a_timeout_by_its_type(error: TimeoutError) -> None:
    """A timeout is recognized by its type, with or without text."""
    assert get_user_friendly_error_message(error, "bot") == "[bot] ⏰ Request timed out. Please try again."


def test_missing_provider_key_points_to_dashboard_setup(tmp_path: Path) -> None:
    """A missing provider key tells the user where to connect one instead of showing the raw exception."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://42.mindroom.chat/"},
    )

    error = missing_api_key_error("OPENROUTER_API_KEY", provider="OpenRouter")

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
    error = AuthenticationFailed("Incorrect API key provided", provider="OpenAI Chat", status_code=401)

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
