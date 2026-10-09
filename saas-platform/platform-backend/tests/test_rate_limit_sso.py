"""Tests for SSO cookie rate limiting behavior."""

from __future__ import annotations

import ipaddress
import sys

import pytest

# Use proper Stripe mock
from tests.stripe_mock import create_stripe_mock

sys.modules.setdefault("stripe", create_stripe_mock())

from backend import deps  # noqa: E402
from backend.deps import rate_limit_key, verify_user  # noqa: E402
from fastapi import Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


def _override_verify_user() -> dict[str, str]:
    return {"user_id": "test-user", "email": "test@example.com"}


def test_sso_cookie_rate_limit() -> None:
    """31st request within a minute should return 429."""
    app.dependency_overrides[verify_user] = _override_verify_user
    try:
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
    finally:
        app.dependency_overrides.pop(verify_user, None)


def _request(peer: str, headers: dict[str, str]) -> Request:
    return Request(
        {"type": "http", "headers": [(k.encode(), v.encode()) for k, v in headers.items()], "client": (peer, 1)}
    )


@pytest.fixture
def trusted_ingress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "TRUSTED_PROXY_NETWORKS", (ipaddress.ip_network("10.42.0.0/16"),))


@pytest.mark.usefixtures("trusted_ingress")
def test_rate_limit_key_ignores_forwarded_addresses_from_untrusted_peers() -> None:
    """A direct caller cannot pick its rate-limit and lockout key by naming another client."""
    headers = {"x-real-ip": "203.0.113.10", "x-forwarded-for": "203.0.113.10"}

    assert rate_limit_key(_request("198.51.100.50", headers)) == "198.51.100.50"


@pytest.mark.usefixtures("trusted_ingress")
@pytest.mark.parametrize(
    ("real_ip", "key"), [("203.0.113.10", "203.0.113.10"), ("", "10.42.0.7"), ("not-an-ip", "10.42.0.7")]
)
def test_rate_limit_key_uses_the_real_ip_the_trusted_ingress_reports(real_ip: str, key: str) -> None:
    """The ingress overwrites X-Real-IP, while X-Forwarded-For can carry client-supplied hops."""
    headers = {"x-forwarded-for": "198.51.100.50, 10.42.0.7", "x-real-ip": real_ip}

    assert rate_limit_key(_request("10.42.0.7", headers)) == key
