"""Tests for SSO cookie rate limiting behavior."""

from __future__ import annotations

import sys

# Use proper Stripe mock
from tests.stripe_mock import create_stripe_mock

sys.modules.setdefault("stripe", create_stripe_mock())

from backend.deps import rate_limit_key, verify_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


def _override_verify_user() -> dict[str, str]:
    return {"user_id": "test-user", "email": "test@example.com"}


def test_sso_cookie_rate_limit() -> None:
    """31st request within a minute should return 429."""
    app.dependency_overrides[verify_user] = _override_verify_user
    client = TestClient(app)
    headers = {"authorization": "Bearer test-token"}

    # This endpoint is hit during OAuth completion and dashboard mount.
    # Keep the limit high enough for retries while still bounding abuse.
    statuses = []
    for _ in range(31):
        r = client.post("/my/sso-cookie", headers=headers, data="ok")
        statuses.append(r.status_code)

    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429


def test_rate_limit_key_prefers_forwarded_client_ip() -> None:
    """Rate limiting should not collapse all ingress traffic onto the pod IP."""
    request = type(
        "Request",
        (),
        {
            "headers": {"x-forwarded-for": "203.0.113.10, 10.42.0.7"},
            "client": type("Client", (), {"host": "10.42.0.7"})(),
        },
    )()

    assert rate_limit_key(request) == "203.0.113.10"


def test_rate_limit_key_prefers_real_ip_from_trusted_ingress() -> None:
    """Trusted ingress rewrites X-Real-IP, while X-Forwarded-For can be client-supplied."""
    request = type(
        "Request",
        (),
        {
            "headers": {
                "x-forwarded-for": "198.51.100.50, 10.42.0.7",
                "x-real-ip": "203.0.113.10",
            },
            "client": type("Client", (), {"host": "10.42.0.7"})(),
        },
    )()

    assert rate_limit_key(request) == "203.0.113.10"
