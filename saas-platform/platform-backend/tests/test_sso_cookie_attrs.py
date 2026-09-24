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


def _legacy_shared_cookie_domain() -> str | None:
    return sso._legacy_shared_sso_cookie_domain()


def _token_cookie(cookies: list[str]) -> str:
    token_cookies = [cookie for cookie in cookies if cookie.startswith(f"{sso.SSO_COOKIE_NAME}=tok")]
    assert len(token_cookies) == 1
    return token_cookies[0]


def _assert_host_only_expiry_cookie(cookies: list[str]) -> None:
    assert any(
        cookie.startswith(f"{sso.SSO_COOKIE_NAME}=")
        and "domain=" not in cookie.lower()
        and "max-age=0" in cookie.lower()
        for cookie in cookies
    )


def _assert_legacy_shared_expiry_cookie(cookies: list[str]) -> None:
    expected_domain = _legacy_shared_cookie_domain()
    assert expected_domain is not None
    assert any(
        cookie.startswith("mindroom_jwt=")
        and f"domain={expected_domain}".lower() in cookie.lower()
        and "max-age=0" in cookie.lower()
        for cookie in cookies
    )


def _client() -> TestClient:
    app.state.limiter = Limiter(key_func=get_remote_address)
    app.state.limiter.reset()
    limiter.reset()
    return TestClient(app)


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("mindroom.chat", ".mindroom.chat"),
        (".mindroom.chat", ".mindroom.chat"),
        ("api.mindroom.chat", ".api.mindroom.chat"),
    ],
)
def test_legacy_shared_cookie_domain_matches_old_dns_cookies(
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    expected: str,
) -> None:
    """Browser-valid DNS domains used to receive shared-domain cookies that must now be expired."""
    monkeypatch.setattr(sso, "PLATFORM_DOMAIN", domain)

    assert sso._legacy_shared_sso_cookie_domain() == expected


@pytest.mark.parametrize(
    "domain",
    ["", " ", "localhost", ".localhost", "127.0.0.1", "192.168.1.10", "::1", "2001:db8::1", "internal"],
)
def test_legacy_shared_cookie_domain_omits_invalid_browser_domains(
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
) -> None:
    """Local, IP, and single-label hosts never had shared-domain cookies."""
    monkeypatch.setattr(sso, "PLATFORM_DOMAIN", domain)

    assert sso._legacy_shared_sso_cookie_domain() is None


def test_sso_cookie_has_security_flags() -> None:
    """Check SSO Set-Cookie includes HttpOnly, Secure and SameSite=Lax."""
    app.dependency_overrides[verify_user] = _override_verify_user
    client = _client()
    # Use a unique client IP to avoid interference with rate-limit tests
    r = client.post(
        "/my/sso-cookie",
        headers={"authorization": "Bearer tok", "X-Forwarded-For": "10.1.2.3"},
        content="x",
    )
    assert r.status_code == 200
    set_cookie = _token_cookie(r.headers.get_list("set-cookie")).lower()
    # Basic flags
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie
    assert set_cookie.startswith("__host-mindroom_jwt=")


def test_sso_cookie_returns_401_without_bearer_token() -> None:
    """Missing bearer tokens should be reported by the SSO cookie route."""
    app.dependency_overrides[verify_user] = _override_verify_user
    client = _client()

    response = client.post("/my/sso-cookie", headers={"X-Forwarded-For": "10.1.2.6"}, content="x")

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing bearer token"}


def test_sso_cookie_stays_on_platform_api_host() -> None:
    """The platform token cookie is host-only, so tenant subdomains never receive it."""
    app.dependency_overrides[verify_user] = _override_verify_user
    client = _client()

    response = client.post(
        "/my/sso-cookie", headers={"authorization": "Bearer tok", "X-Forwarded-For": "10.1.2.4"}, content="x"
    )

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert "domain=" not in _token_cookie(cookies).lower()
    assert not any("domain=" in cookie.lower() and "max-age=0" not in cookie.lower() for cookie in cookies)
    _assert_legacy_shared_expiry_cookie(cookies)


def test_sso_cookie_omits_legacy_expiry_without_shared_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local platform domains only receive the host-only token cookie."""
    monkeypatch.setattr(sso, "PLATFORM_DOMAIN", "localhost")
    app.dependency_overrides[verify_user] = _override_verify_user
    client = _client()

    response = client.post(
        "/my/sso-cookie", headers={"authorization": "Bearer tok", "X-Forwarded-For": "10.1.2.8"}, content="x"
    )

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    assert "domain=" not in _token_cookie(cookies).lower()


def test_clear_sso_cookie_clears_host_only_and_legacy_shared_cookie() -> None:
    """Logout clears the host-only cookie and the legacy shared-domain cookie."""
    client = _client()

    response = client.delete("/my/sso-cookie", headers={"X-Forwarded-For": "10.1.2.5"})

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    _assert_host_only_expiry_cookie(cookies)
    _assert_legacy_shared_expiry_cookie(cookies)


def test_clear_sso_cookie_omits_domain_cookie_without_shared_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Logout should not emit invalid domain cookies for local platform domains."""
    monkeypatch.setattr(sso, "PLATFORM_DOMAIN", "localhost")
    client = _client()

    response = client.delete("/my/sso-cookie", headers={"X-Forwarded-For": "10.1.2.7"})

    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    _assert_host_only_expiry_cookie(cookies)
