"""Tests for SSO cookie attributes (security flags)."""

from __future__ import annotations

import sys

# Use proper Stripe mock
from tests.stripe_mock import create_stripe_mock

sys.modules.setdefault("stripe", create_stripe_mock())

from backend.deps import Limiter, get_remote_address, limiter, verify_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


def _override_verify_user() -> dict[str, str]:
    return {"user_id": "test-user", "email": "test@example.com"}


def test_sso_cookie_has_security_flags() -> None:
    """Check SSO Set-Cookie includes HttpOnly, Secure and SameSite=Lax."""
    app.dependency_overrides[verify_user] = _override_verify_user
    # Reset rate limiter state for this endpoint to avoid cross-test bleed
    app.state.limiter = Limiter(key_func=get_remote_address)
    # Reset both app limiter instance and the global limiter used by decorators
    app.state.limiter.reset()
    limiter.reset()
    client = TestClient(app)
    # Use a unique client IP to avoid interference with rate-limit tests
    r = client.post(
        "/my/sso-cookie",
        headers={"authorization": "Bearer tok", "X-Forwarded-For": "10.1.2.3"},
        data="x",
    )
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie") or ""
    # Basic flags
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    # Starlette normalizes to lowercase in some backends
    assert "samesite=lax" in set_cookie.lower()
