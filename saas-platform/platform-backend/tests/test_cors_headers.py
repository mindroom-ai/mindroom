"""CORS header behavior tests."""

from __future__ import annotations

import os
import sys
from fastapi.testclient import TestClient


def test_cors_headers_present_for_allowed_origin() -> None:
    """Allowed origin should be echoed and credentials allowed."""
    # Configure test environment
    os.environ["PLATFORM_DOMAIN"] = "test.mindroom.chat"
    os.environ["ENVIRONMENT"] = "test"

    # Force reimport of modules to pick up new environment
    if "backend.config" in sys.modules:
        del sys.modules["backend.config"]
    if "main" in sys.modules:
        del sys.modules["main"]

    # Now import with correct environment
    from main import app

    client = TestClient(app)
    origin = "https://app.test.mindroom.chat"

    # Simple request (GET) should include CORS headers for allowed origin
    r = client.get("/health", headers={"Origin": origin})

    assert r.status_code in (200, 206, 207)
    # Starlette lowercases header keys in the client
    assert r.headers.get("access-control-allow-origin") == origin
    assert r.headers.get("access-control-allow-credentials") == "true"
