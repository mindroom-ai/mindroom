"""Tests for SSO cookie rate limiting behavior."""

from __future__ import annotations

import sys
import types

# Stub external deps not needed for this test
sys.modules.setdefault("stripe", types.SimpleNamespace(api_key=""))

from backend.deps import verify_user  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


def _override_verify_user() -> dict[str, str]:
    return {"user_id": "test-user", "email": "test@example.com"}


def test_sso_cookie_rate_limit() -> None:
    """6th request within a minute should return 429."""
    app.dependency_overrides[verify_user] = _override_verify_user
    client = TestClient(app)
    headers = {"authorization": "Bearer test-token"}

    # Limit is 5/min; 6th request should be 429
    statuses = []
    for _ in range(6):
        r = client.post("/my/sso-cookie", headers=headers, data="ok")
        statuses.append(r.status_code)

    assert statuses[:5] == [200, 200, 200, 200, 200]
    assert statuses[5] == 429
