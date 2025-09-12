"""Tests for admin resource allowlist and error handling."""

from __future__ import annotations

import sys
import types

# Stub external deps not needed for this test
sys.modules.setdefault("stripe", types.SimpleNamespace(api_key=""))

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
