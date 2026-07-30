"""Tests for SSO cookie attributes (security flags)."""

from __future__ import annotations

import sys

# Use proper Stripe mock
from tests.stripe_mock import create_stripe_mock

sys.modules.setdefault("stripe", create_stripe_mock())

import pytest  # noqa: E402
from backend.deps import Limiter, get_remote_address, limiter, verify_user  # noqa: E402
from backend.routes import sso  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.pop(verify_user, None)


def _override_verify_user() -> dict[str, str]:
    return {"user_id": "test-user", "email": "test@example.com"}


def _expected_cookie_domain() -> str | None:
    return sso._sso_cookie_domain()


def _token_cookie(cookies: list[str]) -> str:
    token_cookies = [cookie for cookie in cookies if cookie.startswith("mindroom_jwt=tok")]
    assert len(token_cookies) == 1
    return token_cookies[0]


def _assert_host_only_expiry_cookie(cookies: list[str]) -> None:
    assert any(
        cookie.startswith("mindroom_jwt=") and "domain=" not in cookie.lower() and "max-age=0" in cookie.lower()
        for cookie in cookies
    )


def _assert_cookie_domain(cookie: str, expected_domain: str | None) -> None:
    cookie_lower = cookie.lower()
    if expected_domain is None:
        assert "domain=" not in cookie_lower
        return
    assert f"domain={expected_domain}".lower() in cookie_lower


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("mindroom.chat", ".mindroom.chat"),
        (".mindroom.chat", ".mindroom.chat"),
        ("api.mindroom.chat", ".api.mindroom.chat"),
    ],
)
def test_sso_cookie_domain_uses_shared_domain_for_dns_hosts(
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    expected: str,
) -> None:
    """Browser-valid DNS domains should produce shared-domain cookies."""
    monkeypatch.setattr(sso, "PLATFORM_DOMAIN", domain)

    assert sso._sso_cookie_domain() == expected


@pytest.mark.parametrize(
    "domain",
    ["", " ", "localhost", ".localhost", "127.0.0.1", "192.168.1.10", "::1", "2001:db8::1", "internal"],
)
def test_sso_cookie_domain_omits_invalid_browser_domains(monkeypatch: pytest.MonkeyPatch, domain: str) -> None:
    """Local, IP, and single-label hosts should fall back to host-only cookies."""
    monkeypatch.setattr(sso, "PLATFORM_DOMAIN", domain)

    assert sso._sso_cookie_domain() is None


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
    set_cookie = _token_cookie(r.headers.get_list("set-cookie")).lower()
    # Basic flags
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie


def test_sso_cookie_returns_401_without_bearer_token() -> None:
    """Missing bearer tokens should be reported by the SSO cookie route."""
    app.dependency_overrides[verify_user] = _override_verify_user
    app.state.limiter = Limiter(key_func=get_remote_address)
    app.state.limiter.reset()
    limiter.reset()
    client = TestClient(app)

    response = client.post("/my/sso-cookie", headers={"X-Forwarded-For": "10.1.2.6"}, data="x")

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing bearer token"}


def test_sso_cookie_is_shared_with_tenant_subdomains() -> None:
    """SSO token cookie must be visible to hosted tenant subdomains."""
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
    _assert_cookie_domain(_token_cookie(cookies), _expected_cookie_domain())
    _assert_host_only_expiry_cookie(cookies)


def test_clear_sso_cookie_clears_domain_and_legacy_host_only_cookie() -> None:
    """Logout clears the shared-domain cookie and the legacy host-only cookie."""
    app.state.limiter = Limiter(key_func=get_remote_address)
    app.state.limiter.reset()
    limiter.reset()
    client = TestClient(app)

    response = client.delete("/my/sso-cookie", headers={"X-Forwarded-For": "10.1.2.5"})

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    _assert_host_only_expiry_cookie(cookies)
    expected_domain = _expected_cookie_domain()
    assert expected_domain is not None
    assert any(
        cookie.startswith("mindroom_jwt=")
        and f"domain={expected_domain}".lower() in cookie.lower()
        and "max-age=0" in cookie.lower()
        for cookie in cookies
    )


def test_clear_sso_cookie_omits_domain_cookie_without_shared_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logout should not emit invalid domain cookies for local platform domains."""
    monkeypatch.setattr(sso, "PLATFORM_DOMAIN", "localhost")
    app.state.limiter = Limiter(key_func=get_remote_address)
    app.state.limiter.reset()
    limiter.reset()
    client = TestClient(app)

    response = client.delete("/my/sso-cookie", headers={"X-Forwarded-For": "10.1.2.7"})

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    _assert_host_only_expiry_cookie(cookies)
