"""Tests for SSO cookie attributes (security flags)."""

from __future__ import annotations

import sys
import types

sys.modules.setdefault("stripe", types.SimpleNamespace(api_key=""))

from backend.deps import verify_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


def _override_verify_user() -> dict[str, str]:
    return {"user_id": "test-user", "email": "test@example.com"}


def test_sso_cookie_has_security_flags() -> None:
    """Check SSO Set-Cookie includes HttpOnly, Secure and SameSite=Lax."""
    app.dependency_overrides[verify_user] = _override_verify_user
    client = TestClient(app)
    r = client.post("/my/sso-cookie", headers={"authorization": "Bearer tok"}, data="x")
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie") or ""
    # Basic flags
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    # Starlette normalizes to lowercase in some backends
    assert "samesite=lax" in set_cookie.lower()
