"""Tests for admin resource allowlist and error handling."""

from __future__ import annotations

import sys

# Use proper Stripe mock
from tests.stripe_mock import create_stripe_mock

sys.modules.setdefault("stripe", create_stripe_mock())

from backend.deps import verify_admin  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from main import app  # noqa: E402


def _override_verify_admin() -> dict[str, str]:
    return {"user_id": "admin-user", "email": "admin@example.com"}


def test_admin_allowlist_blocks_unknown_resource() -> None:
    """Unknown admin resources must be rejected with 400."""
    app.dependency_overrides[verify_admin] = _override_verify_admin
    client = TestClient(app)
    r = client.get("/admin/unknown_resource")
    assert r.status_code == 400
    assert r.json().get("detail") == "Invalid resource"
