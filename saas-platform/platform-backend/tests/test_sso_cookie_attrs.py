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
    r = client.post("/my/sso-cookie", headers={"authorization": "Bearer tok", "X-Forwarded-For": "10.1.2.3"}, data="x")
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie") or ""
    # Basic flags
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    # Starlette normalizes to lowercase in some backends
    assert "samesite=lax" in set_cookie.lower()


def test_sso_cookie_is_host_only() -> None:
    """SSO token cookie must stay on the API host, not every tenant subdomain."""
    app.dependency_overrides[verify_user] = _override_verify_user
    app.state.limiter = Limiter(key_func=get_remote_address)
    app.state.limiter.reset()
    limiter.reset()
    client = TestClient(app)

    response = client.post(
        "/my/sso-cookie", headers={"authorization": "Bearer tok", "X-Forwarded-For": "10.1.2.4"}, data="x"
    )

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    token_cookies = [cookie for cookie in cookies if cookie.startswith("mindroom_jwt=tok")]
    assert len(token_cookies) == 1
    assert "Domain=" not in token_cookies[0]
    assert all("Domain=" not in cookie for cookie in cookies)


def test_clear_sso_cookie_clears_host_only_cookie() -> None:
    """Logout clears the current host-only cookie."""
    app.state.limiter = Limiter(key_func=get_remote_address)
    app.state.limiter.reset()
    limiter.reset()
    client = TestClient(app)

    response = client.delete("/my/sso-cookie", headers={"X-Forwarded-For": "10.1.2.5"})

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert any(
        cookie.startswith("mindroom_jwt=") and "Domain=" not in cookie and "Max-Age=0" in cookie for cookie in cookies
    )
    assert all("Domain=" not in cookie for cookie in cookies)
