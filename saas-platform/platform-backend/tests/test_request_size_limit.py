"""Tests for request size limiting middleware."""

from __future__ import annotations

from backend.deps import verify_user
from fastapi.testclient import TestClient
from main import app


def _override_verify_user() -> dict[str, str]:
    return {"user_id": "test-user", "email": "test@example.com"}


def test_request_too_large_returns_413() -> None:
    """Payload >1 MiB should be rejected with 413."""
    app.dependency_overrides[verify_user] = _override_verify_user
    client = TestClient(app)

    # Build a payload larger than 1 MiB
    big = "x" * (1024 * 1024 + 100)
    headers = {
        "Authorization": "Bearer test-token",
        "Content-Length": str(len(big)),  # Explicitly set Content-Length
    }
    r = client.post("/my/sso-cookie", headers=headers, data=big)
    assert r.status_code == 413
    assert r.json().get("detail") == "Request too large"
