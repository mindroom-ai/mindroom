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
    try:
        # Reset rate limiter state for this endpoint to avoid cross-test bleed
        app.state.limiter = Limiter(key_func=get_remote_address)
        # Reset both app limiter instance and the global limiter used by decorators
        app.state.limiter.reset()
        limiter.reset()
        client = TestClient(app)
        # Use a unique client IP to avoid interference with rate-limit tests
        r = client.post(
            "/my/sso-cookie", headers={"authorization": "Bearer tok", "X-Forwarded-For": "10.1.2.3"}, data="x"
        )
        assert r.status_code == 200
        set_cookie = r.headers.get("set-cookie") or ""
        # Basic flags
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        # Starlette normalizes to lowercase in some backends
        assert "samesite=lax" in set_cookie.lower()
    finally:
        app.dependency_overrides.pop(verify_user, None)


def test_sso_cookie_keeps_raw_token_host_only() -> None:
    """Raw platform JWT cookie should not be scoped to tenant subdomains."""
    app.dependency_overrides[verify_user] = _override_verify_user
    try:
        app.state.limiter = Limiter(key_func=get_remote_address)
        app.state.limiter.reset()
        limiter.reset()
        client = TestClient(app)

        r = client.post(
            "/my/sso-cookie", headers={"authorization": "Bearer raw-platform-jwt", "X-Forwarded-For": "10.1.2.4"}
        )

        assert r.status_code == 200
        raw_token_cookies = [
            header for header in r.headers.get_list("set-cookie") if header.startswith("mindroom_jwt=raw-platform-jwt")
        ]
        assert raw_token_cookies
        assert all("domain=" not in header.lower() for header in raw_token_cookies)
    finally:
        app.dependency_overrides.pop(verify_user, None)
