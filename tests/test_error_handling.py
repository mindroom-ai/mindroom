"""Tests for error handling module."""

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from mindroom import constants
from mindroom.error_handling import (
    ReplyErrorInfo,
    describe_reply_error,
    get_user_friendly_error_message,
    minimal_mode_failure_message,
    model_error_labels,
)
from mindroom.llm.errors import (
    MISSING_API_KEY,
    MODEL_SAFEGUARD_REFUSAL_MESSAGE,
    QUOTA_EXHAUSTED,
    STREAM_STALLED,
    TOKENS_PER_MINUTE,
    AuthenticationFailed,
    ContextWindowExceeded,
    InvalidRequest,
    ModelSafeguardRefusal,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.usage import MessageMetrics
from mindroom.providers.errors import classify_provider_error, missing_api_key_error
from mindroom.tool_system.runtime_context import detached_tool_scope
from mindroom.tool_system.worker_routing import build_tool_execution_identity


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
    assert get_user_friendly_error_message(RuntimeError(text), "assistant") == f"[assistant] ⚠️ {text}"


def test_an_error_reply_redacts_secrets() -> None:
    """Error text is redacted before it becomes Matrix-visible reply text or structured error metadata."""
    error = AuthenticationFailed("Incorrect API key provided: sk-test-secret", provider="OpenAI Chat", status_code=401)

    message = get_user_friendly_error_message(error, "assistant")
    info = describe_reply_error(error)

    assert message.startswith("[assistant] ❌ Authentication failed (OpenAI Chat): ")
    assert "***redacted***" in message
    assert "sk-test-secret" not in message
    assert "sk-test-secret" not in str(info.to_wire())
    assert info.detail is None


def test_model_safeguard_refusal_gives_actionable_guidance() -> None:
    """An explicit safeguard stop should not look like an empty model response."""
    error = ModelSafeguardRefusal(provider="Anthropic", usage=MessageMetrics())

    message = get_user_friendly_error_message(error, "mind")

    assert message == (
        "[mind] ⚠️ Blocked by model safeguards — The model's safeguards blocked this request. "
        "Choose another model (/model list) or revise the prompt."
    )
    assert "stop_reason" not in message


def test_model_safeguard_refusal_includes_provider_code_when_present() -> None:
    """A safeguard refusal carrying a native provider code names that code in the user-facing message."""
    error = ModelSafeguardRefusal(provider="openrouter", usage=MessageMetrics(), provider_code="RECITATION")

    message = get_user_friendly_error_message(error, "mind", model_label="Neon")
    info = describe_reply_error(error, model_label="Neon")

    assert message == (
        "[mind] ⚠️ Blocked by model safeguards — Neon's safeguards blocked this request (RECITATION). "
        "Choose another model (/model list) or revise the prompt."
    )
    assert info.kind == "safeguard_refusal"
    assert info.code == "RECITATION"
    assert info.provider == "OpenRouter"
    assert info.retryable is False
    assert info.action is None


def test_only_the_typed_refusal_gets_the_refusal_reply() -> None:
    """An error that merely carries the refusal's text is no refusal, since every refusal reaches the reply typed."""
    error = RuntimeError(MODEL_SAFEGUARD_REFUSAL_MESSAGE)

    assert get_user_friendly_error_message(error) == f"⚠️ {MODEL_SAFEGUARD_REFUSAL_MESSAGE}"


# How the requests transport starts its report when the Google sign-in host cannot be reached.
_OAUTH_HOST_UNREACHABLE = (
    "HTTPSConnectionPool(host='oauth2.googleapis.com', port=443): Max retries exceeded with url: /token"
)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ProviderUnavailable(_OAUTH_HOST_UNREACHABLE, provider="VertexAI", code="connection_error"),
            "⚠️ VertexAI unavailable — The model could not complete the reply after automatic retries.",
            id="unavailable-host-name-holds-auth",
        ),
        pytest.param(
            ProviderUnavailable("upstream replied 401 from the gateway", provider="OpenAI", status_code=503),
            "⚠️ OpenAI unavailable (503) — The model could not complete the reply after automatic retries.",
            id="unavailable-wording-holds-401",
        ),
        pytest.param(
            ProviderUnavailable("failed to generate a response", provider="OpenAI", status_code=500),
            "⚠️ OpenAI unavailable (500) — The model could not complete the reply after automatic retries.",
            id="unavailable-wording-holds-rate",
        ),
        pytest.param(
            RateLimited("oauth2 gateway throttled the request", provider="OpenAI", status_code=429),
            "⏱️ Rate limit reached — The model hit OpenAI's rate limit after automatic retries.",
            id="rate-limited-wording-holds-auth",
        ),
        pytest.param(
            RateLimited("upstream unavailable", provider="OpenAI", status_code=429),
            "⏱️ Rate limit reached — The model hit OpenAI's rate limit after automatic retries.",
            id="rate-limited-generic-wording",
        ),
        pytest.param(
            ProviderError(
                "Project proj_1 does not have access to model gpt-6-astra",
                provider="OpenAI",
                status_code=403,
            ),
            "⚠️ Project proj_1 does not have access to model gpt-6-astra",
            id="final",
        ),
        pytest.param(
            ProviderError("upstream unavailable", provider="OpenAI", status_code=503),
            "⚠️ upstream unavailable",
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
            "⚠️ models/gemini-x is not supported for generateContent. Call ListModels to see the api_key methods.",
            id="invalid-request-wording-is-not-read-again",
        ),
        pytest.param(
            ProviderError("Request timeout while the provider read the body", provider="Ollama"),
            "⚠️ Request timeout while the provider read the body",
            id="final-timeout-wording",
        ),
        pytest.param(
            ContextWindowExceeded("prompt is too long", provider="Anthropic", status_code=400),
            "⚠️ Context window full — This conversation exceeded the model's context window. "
            "Compact the thread or start a new one.",
            id="context-window",
        ),
        pytest.param(
            ContextWindowExceeded(
                "Request too large for gpt-6-astra on tokens per min (TPM)",
                provider="OpenAI",
                status_code=429,
                code=TOKENS_PER_MINUTE,
            ),
            "⚠️ Request too large for rate limit — This request exceeded the account's tokens-per-minute limit. "
            "Compact the thread or start a new one.",
            id="over-tokens-per-minute",
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
    assert message == "⚠️ Something went wrong"


def test_an_error_without_text_is_reported_by_its_type_name() -> None:
    """An exception without text, such as one an operation raises bare, names its type instead of nothing."""
    assert get_user_friendly_error_message(ConnectionResetError(), "bot") == "[bot] ⚠️ ConnectionResetError"


@pytest.mark.parametrize("error", [TimeoutError(), TimeoutError("lock wait expired")])
def test_a_timeout_reads_as_a_timeout_by_its_type(error: TimeoutError) -> None:
    """A timeout is recognized by its type, with or without text."""
    assert (
        get_user_friendly_error_message(error, "bot")
        == "[bot] ⏰ Request timed out — The model timed out waiting for a response. Try again shortly."
    )


def test_missing_provider_key_points_to_dashboard_setup(tmp_path: Path) -> None:
    """A missing provider key tells the user where to connect one instead of showing the raw exception."""
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MINDROOM_PUBLIC_URL": "https://42.mindroom.chat/"},
    )

    error = missing_api_key_error("OPENROUTER_API_KEY", provider="OpenRouter")

    message = get_user_friendly_error_message(error, "mind", runtime_paths=runtime_paths)
    info = describe_reply_error(error, runtime_paths=runtime_paths)

    assert message == (
        "[mind] 🔑 No AI provider key is set up yet, so I can't reply. "
        "Open the MindRoom dashboard (https://42.mindroom.chat), choose **Connect your AI provider**, "
        "paste your key, then send your message again."
    )
    assert info.kind == "missing_api_key"
    assert info.title == "No AI provider key configured"
    assert not info.summary.startswith("🔑")
    assert info.code == MISSING_API_KEY


def test_missing_provider_key_without_public_url_names_dashboard() -> None:
    """Without a known dashboard URL the message still points at the dashboard setup step."""
    error = missing_api_key_error("OPENROUTER_API_KEY", provider="OpenRouter")

    message = get_user_friendly_error_message(error)

    assert "Open the MindRoom dashboard, choose **Connect your AI provider**" in message
    assert "OPENROUTER_API_KEY" not in message


def test_headless_missing_key_reply_keeps_the_error_its_own_sentence(tmp_path: Path) -> None:
    """A headless run's reply ends the provider's own text before naming the `.env` file, whatever it ended with."""
    runtime_paths = constants.resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path)
    identity = build_tool_execution_identity(
        channel="headless",
        agent_name="coder",
        runtime_paths=runtime_paths,
        requester_id="@alice:localhost",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="headless:1",
    )
    error = AuthenticationFailed(
        "Azure OpenAI authentication not configured. Please provide one of: azure_ad_token_provider",
        provider="Azure",
        status_code=401,
        code=MISSING_API_KEY,
    )
    with detached_tool_scope(identity):
        message = get_user_friendly_error_message(error, runtime_paths=runtime_paths)

    assert f"azure_ad_token_provider. A `.env` file at {tmp_path / '.env'} works too." in message


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
        "Minimal Bash requires an active managed response owner. Return to standard mode with `/mode helper standard`."
    )


@pytest.mark.parametrize(
    ("status_code", "code", "message"),
    [
        pytest.param(
            402,
            None,
            "Insufficient credits. Add more using https://openrouter.ai/settings/credits",
            id="openrouter-402",
        ),
        pytest.param(
            403,
            None,
            "Key limit exceeded (monthly limit). Manage it using https://openrouter.ai/settings/keys",
            id="openrouter-key-limit",
        ),
        pytest.param(402, None, "Insufficient Balance", id="deepseek"),
        pytest.param(429, "1113", "Insufficient balance or no resource package. Please recharge.", id="zai"),
        pytest.param(429, "1309", "Your GLM Coding Plan package has expired.", id="zai-plan-expired"),
        pytest.param(429, "1314", "Your enterprise package has expired.", id="zai-package-expired"),
    ],
)
def test_a_used_up_ai_budget_says_so_without_the_providers_billing_text(
    status_code: int,
    code: str | None,
    message: str,
) -> None:
    """A provider refusing for lack of credit gets one plain reply, not its own text and billing links."""
    error = classify_provider_error(message, provider="openrouter", status_code=status_code, code=code)
    reply = get_user_friendly_error_message(error, "assistant")
    info = describe_reply_error(error)
    assert reply == (
        "[assistant] 💳 Account quota reached — "
        "OpenRouter rejected this request because the account's quota or credits are exhausted."
    )
    assert "openrouter.ai" not in reply
    assert info.kind == "quota_exhausted"
    assert info.code == QUOTA_EXHAUSTED
    assert info.provider == "OpenRouter"
    assert info.retryable is False
    assert info.action is None


@pytest.mark.parametrize(
    ("error_text", "expected_summary"),
    [
        pytest.param(
            "Model stream stalled: no provider event for 300s",
            "OpenRouter sent no data for 5 min (retried once automatically).",
            id="exact-minutes",
        ),
        pytest.param(
            "Model stream stalled: no provider event for 90s",
            "OpenRouter sent no data for 1 min 30s (retried once automatically).",
            id="minutes-and-seconds",
        ),
        pytest.param(
            "Model stream stalled: no provider event for 45s",
            "OpenRouter sent no data for 45s (retried once automatically).",
            id="sub-minute",
        ),
        pytest.param(
            "stream stalled",
            "OpenRouter stopped sending data after automatic retry.",
            id="without-duration",
        ),
    ],
)
def test_stream_stalled_formats_duration_and_model_label(error_text: str, expected_summary: str) -> None:
    """A stalled stream reports the model name, provider, human duration, and continue action."""
    error = ProviderUnavailable(error_text, provider="OpenRouter", status_code=504, code=STREAM_STALLED)
    info = describe_reply_error(error, model_label="Neon")

    assert info.to_wire() == {
        "kind": "stream_stalled",
        "title": "Neon stopped responding",
        "summary": expected_summary,
        "detail": error_text,
        "code": STREAM_STALLED,
        "status_code": 504,
        "provider": "OpenRouter",
        "retryable": True,
        "action": "continue",
    }
    assert info.plain_text == f"⚠️ Neon stopped responding — {expected_summary}"


def test_describe_reply_error_covers_every_kind_and_omits_empty_wire_fields() -> None:
    """Every ReplyErrorInfo kind carries its exact retryable/action contract and omits None/empty keys."""
    cases: list[tuple[Exception, str, bool, str | None]] = [
        (ModelSafeguardRefusal(provider="Anthropic", usage=MessageMetrics()), "safeguard_refusal", False, None),
        (missing_api_key_error("OPENAI_API_KEY", provider="OpenAI"), "missing_api_key", False, None),
        (
            ProviderError("out of credit", provider="OpenRouter", status_code=402, code=QUOTA_EXHAUSTED),
            "quota_exhausted",
            False,
            None,
        ),
        (AuthenticationFailed("bad key", provider="OpenAI", status_code=401), "authentication_failed", False, None),
        (
            ContextWindowExceeded("too big for tpm", provider="OpenAI", status_code=429, code=TOKENS_PER_MINUTE),
            "tokens_per_minute",
            False,
            "compact",
        ),
        (
            ContextWindowExceeded("prompt too long", provider="Anthropic", status_code=400),
            "context_window",
            False,
            "compact",
        ),
        (RateLimited("429", provider="OpenRouter", status_code=429), "rate_limited", True, "retry"),
        (
            ProviderUnavailable(
                "no provider event for 300s",
                provider="OpenRouter",
                status_code=504,
                code=STREAM_STALLED,
            ),
            "stream_stalled",
            True,
            "continue",
        ),
        (ProviderUnavailable("503", provider="OpenRouter", status_code=503), "provider_unavailable", True, "continue"),
        (TimeoutError("timed out waiting"), "timeout", True, "retry"),
        (RuntimeError("unexpected failure"), "runtime_error", False, None),
    ]
    for error, kind, retryable, action in cases:
        info = describe_reply_error(error, model_label="Neon", provider="openrouter")
        wire = info.to_wire()
        assert isinstance(info, ReplyErrorInfo)
        assert wire["kind"] == kind
        assert wire["retryable"] is retryable
        assert wire.get("action") == action
        for optional_key in ("detail", "code", "status_code", "provider", "action"):
            if optional_key in wire:
                assert wire[optional_key] not in (None, "")

    # Non-ProviderError failures never blame the configured model provider.
    assert (
        "provider"
        not in describe_reply_error(RuntimeError("boom"), model_label="Neon", provider="openrouter").to_wire()
    )
    assert (
        "provider"
        not in describe_reply_error(TimeoutError("lock timeout"), model_label="Neon", provider="openrouter").to_wire()
    )
    # Redaction applies to detail too, not just summary.
    secret_unavailable = ProviderUnavailable("gateway rejected sk-live-secret", provider="OpenRouter", status_code=503)
    wire = describe_reply_error(secret_unavailable, model_label="Neon").to_wire()
    assert "sk-live-secret" not in str(wire)
    assert "***redacted***" in wire["detail"]


def test_include_agent_prefix_false_omits_prefix_while_preserving_structured_logging() -> None:
    """Own-user replies drop the redundant [agent_name] prefix while still logging agent=<agent_name>."""
    error = ProviderUnavailable(
        "Model stream stalled: no provider event for 300s",
        provider="OpenRouter",
        status_code=504,
        code=STREAM_STALLED,
    )
    with capture_logs() as logs:
        message = get_user_friendly_error_message(
            error,
            "mindroom_dev",
            model_label="Neon",
            include_agent_prefix=False,
        )

    assert message == "⚠️ Neon stopped responding — OpenRouter sent no data for 5 min (retried once automatically)."
    assert not message.startswith("[mindroom_dev]")
    [entry] = [log for log in logs if log["event"] == "agent_error"]
    assert entry["agent"] == "mindroom_dev"
    assert entry["error_code"] == STREAM_STALLED
    assert entry["status_code"] == 504


def test_model_error_labels_prefers_display_name_over_id() -> None:
    """Published model payloads resolve display_name first, falling back to model id."""
    assert model_error_labels(
        {"config": "neon", "display_name": "Neon", "id": "google/gemini-4-neon", "provider": "openrouter"},
    ) == (
        "Neon",
        "openrouter",
    )
    assert model_error_labels({"config": "default", "id": "gpt-6", "provider": "openai"}) == ("gpt-6", "openai")
    assert model_error_labels(None) == (None, None)
