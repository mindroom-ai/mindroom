"""Tests for OpenRouter provisioning support."""

import json
from typing import Any

import pytest

from backend.openrouter import (
    CreatedOpenRouterKey,
    OpenRouterConfigurationError,
    OpenRouterError,
    OpenRouterKeyPlan,
    create_openrouter_key,
)


def test_create_openrouter_key_posts_monthly_spend_limit() -> None:
    """OpenRouter provisioning should create a monthly-limited customer key."""
    captured: dict[str, Any] = {}

    def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(body.decode("utf-8"))
        return (
            201,
            json.dumps(
                {
                    "key": "sk-or-v1-customer-secret",
                    "data": {
                        "hash": "hash_123",
                        "label": "MindRoom instance 42",
                        "limit": 15,
                        "limit_remaining": 15,
                        "limit_reset": "monthly",
                    },
                }
            ).encode("utf-8"),
        )

    result = create_openrouter_key(
        management_api_key="sk-or-v1-management",
        plan=OpenRouterKeyPlan(name="MindRoom instance 42", monthly_limit_usd=15),
        http_post=http_post,
    )

    assert result == CreatedOpenRouterKey(
        key="sk-or-v1-customer-secret",
        hash="hash_123",
        label="MindRoom instance 42",
        limit_usd=15,
        limit_reset="monthly",
    )
    assert captured["url"] == "https://openrouter.ai/api/v1/keys"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-v1-management"
    assert captured["body"] == {
        "name": "MindRoom instance 42",
        "limit": 15,
        "limit_reset": "monthly",
        "include_byok_in_limit": True,
    }


def test_create_openrouter_key_rejects_missing_management_key() -> None:
    """Provisioning should fail before making a request when management auth is absent."""
    calls = 0

    def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return 201, b"{}"

    with pytest.raises(OpenRouterConfigurationError, match="OPENROUTER_PROVISIONING_API_KEY"):
        create_openrouter_key(
            management_api_key="",
            plan=OpenRouterKeyPlan(name="MindRoom instance 42", monthly_limit_usd=15),
            http_post=http_post,
        )

    assert calls == 0


@pytest.mark.parametrize("monthly_limit_usd", [0, -1])
def test_create_openrouter_key_rejects_non_positive_budget(monthly_limit_usd: int) -> None:
    """Invalid provisioning budgets should fail before making an OpenRouter request."""
    calls = 0

    def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:  # noqa: ARG001
        nonlocal calls
        calls += 1
        return 201, b"{}"

    with pytest.raises(OpenRouterError, match="monthly_limit_usd must be greater than 0"):
        create_openrouter_key(
            management_api_key="sk-or-v1-management",
            plan=OpenRouterKeyPlan(name="MindRoom instance 42", monthly_limit_usd=monthly_limit_usd),
            http_post=http_post,
        )

    assert calls == 0


def test_create_openrouter_key_rejects_error_response() -> None:
    """OpenRouter error responses should not leak secret values."""

    def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:  # noqa: ARG001
        return 403, b'{"error":{"message":"Only management keys can perform this operation"}}'

    with pytest.raises(
        OpenRouterError,
        match='OpenRouter key creation failed with status 403: {"error":{"message":"Only management keys',
    ):
        create_openrouter_key(
            management_api_key="sk-or-v1-management",
            plan=OpenRouterKeyPlan(name="MindRoom instance 42", monthly_limit_usd=15),
            http_post=http_post,
        )


def test_create_openrouter_key_rejects_malformed_success_response() -> None:
    """Malformed OpenRouter success responses should fail with an operator-readable error."""

    def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:  # noqa: ARG001
        return 201, b'{"data":{"hash":"hash_123"}}'

    with pytest.raises(OpenRouterError, match="missing field: key"):
        create_openrouter_key(
            management_api_key="sk-or-v1-management",
            plan=OpenRouterKeyPlan(name="MindRoom instance 42", monthly_limit_usd=15),
            http_post=http_post,
        )


def test_create_openrouter_key_rejects_invalid_limit_value() -> None:
    """Invalid typed fields in OpenRouter responses should not crash with raw exceptions."""

    def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:  # noqa: ARG001
        return (
            201,
            json.dumps(
                {
                    "key": "sk-or-v1-customer-secret",
                    "data": {
                        "hash": "hash_123",
                        "label": "MindRoom instance 42",
                        "limit": "not-a-number",
                        "limit_reset": "monthly",
                    },
                }
            ).encode("utf-8"),
        )

    with pytest.raises(OpenRouterError, match="invalid field values"):
        create_openrouter_key(
            management_api_key="sk-or-v1-management",
            plan=OpenRouterKeyPlan(name="MindRoom instance 42", monthly_limit_usd=15),
            http_post=http_post,
        )
