"""Tests for security headers and trusted host enforcement."""

from __future__ import annotations

import sys

# Use proper Stripe mock
from tests.stripe_mock import create_stripe_mock

sys.modules.setdefault("stripe", create_stripe_mock())

from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


def test_security_headers_present() -> None:
    """Ensure security headers are set on a basic request."""
    client = TestClient(app)
    r = client.get("/health")
    # Basic headers
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("x-xss-protection") == "1; mode=block"
    assert "max-age=31536000" in (r.headers.get("strict-transport-security") or "")
    # Additional headers
    assert (r.headers.get("referrer-policy") or "").lower() == "strict-origin-when-cross-origin"
    assert "camera=()" in (r.headers.get("permissions-policy") or "")


def test_trusted_host_rejects_unknown() -> None:
    """Invalid host header must be rejected (400/421)."""
    client = TestClient(app)
    r = client.get("/health", headers={"host": "evil.example.com"})
    assert r.status_code in (400, 421)
