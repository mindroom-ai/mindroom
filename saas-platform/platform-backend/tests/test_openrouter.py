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
    delete_openrouter_key,
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


def test_delete_openrouter_key_sends_management_delete_request() -> None:
    """OpenRouter provisioning should revoke superseded customer keys by hash."""
    captured: dict[str, Any] = {}

    def http_delete(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        captured["url"] = url
        captured["headers"] = headers
        return 200, b'{"deleted":true}'

    delete_openrouter_key(management_api_key="sk-or-v1-management", key_hash="hash_123", http_delete=http_delete)

    assert captured["url"] == "https://openrouter.ai/api/v1/keys/hash_123"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-v1-management"


def test_delete_openrouter_key_default_transport_sends_empty_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenRouter's delete endpoint expects an explicit empty JSON request body."""
    captured: dict[str, Any] = {}

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"deleted":true}'

    def urlopen(request: Any, timeout: int) -> FakeResponse:
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("backend.openrouter.urllib.request.urlopen", urlopen)

    delete_openrouter_key(management_api_key="sk-or-v1-management", key_hash="hash_123")

    assert captured == {"url": "https://openrouter.ai/api/v1/keys/hash_123", "data": b"{}", "timeout": 20}


def test_delete_openrouter_key_reports_openrouter_failures() -> None:
    """Failed OpenRouter key revocation should surface an operator-readable error."""

    def http_delete(url: str, headers: dict[str, str]) -> tuple[int, bytes]:  # noqa: ARG001
        return 404, b'{"error":{"message":"Key not found"}}'

    with pytest.raises(OpenRouterError, match='OpenRouter key deletion failed with status 404: {"error"'):
        delete_openrouter_key(management_api_key="sk-or-v1-management", key_hash="hash_123", http_delete=http_delete)


@pytest.mark.parametrize(
    ("management_api_key", "expected_error"),
    [(None, "OPENROUTER_PROVISIONING_API_KEY must be a string"), ("", "OPENROUTER_PROVISIONING_API_KEY is required")],
)
def test_delete_openrouter_key_rejects_invalid_management_key(management_api_key: object, expected_error: str) -> None:
    """Local delete configuration errors should fail before making an OpenRouter request."""

    def http_delete(url: str, headers: dict[str, str]) -> tuple[int, bytes]:  # noqa: ARG001
        raise AssertionError("delete request should not be sent")

    with pytest.raises(OpenRouterConfigurationError, match=expected_error):
        delete_openrouter_key(
            management_api_key=management_api_key,  # type: ignore[arg-type]
            key_hash="hash_123",
            http_delete=http_delete,
        )


@pytest.mark.parametrize(
    ("key_hash", "expected_error"),
    [(None, "OpenRouter key_hash must be a string"), ("", "OpenRouter key_hash is required")],
)
def test_delete_openrouter_key_rejects_invalid_key_hash(key_hash: object, expected_error: str) -> None:
    """Delete input errors should be reported separately from management-key configuration errors."""

    def http_delete(url: str, headers: dict[str, str]) -> tuple[int, bytes]:  # noqa: ARG001
        raise AssertionError("delete request should not be sent")

    with pytest.raises(OpenRouterError, match=expected_error):
        delete_openrouter_key(
            management_api_key="sk-or-v1-management",
            key_hash=key_hash,  # type: ignore[arg-type]
            http_delete=http_delete,
        )


def test_delete_openrouter_key_rejects_malformed_success_response() -> None:
    """Malformed delete responses should include enough context for operator debugging."""

    def http_delete(url: str, headers: dict[str, str]) -> tuple[int, bytes]:  # noqa: ARG001
        return 200, b"[]"

    with pytest.raises(OpenRouterError, match=r"invalid response.*\[\]"):
        delete_openrouter_key(management_api_key="sk-or-v1-management", key_hash="hash_123", http_delete=http_delete)
