"""Tests for OpenRouter provisioning support."""

import json
from typing import Any

import pytest

from backend.openrouter import CreatedOpenRouterKey, OpenRouterError, OpenRouterKeyPlan, create_openrouter_key


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

    with pytest.raises(OpenRouterError, match="OPENROUTER_PROVISIONING_API_KEY"):
        create_openrouter_key(
            management_api_key="",
            plan=OpenRouterKeyPlan(name="MindRoom instance 42", monthly_limit_usd=15),
            http_post=http_post,
        )

    assert calls == 0


def test_create_openrouter_key_rejects_error_response() -> None:
    """OpenRouter error responses should not leak secret values."""

    def http_post(url: str, headers: dict[str, str], body: bytes) -> tuple[int, bytes]:  # noqa: ARG001
        return 403, b'{"error":{"message":"Only management keys can perform this operation"}}'

    with pytest.raises(OpenRouterError, match="OpenRouter key creation failed with status 403"):
        create_openrouter_key(
            management_api_key="sk-or-v1-management",
            plan=OpenRouterKeyPlan(name="MindRoom instance 42", monthly_limit_usd=15),
            http_post=http_post,
        )
