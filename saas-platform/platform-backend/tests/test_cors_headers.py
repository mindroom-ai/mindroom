"""CORS header behavior tests."""

from __future__ import annotations

from fastapi.testclient import TestClient
from main import app


def test_cors_headers_present_for_allowed_origin() -> None:
    """Allowed origin should be echoed and credentials allowed."""
    client = TestClient(app)
    origin = "https://app.test.mindroom.chat"

    # Simple request (GET) should include CORS headers for allowed origin
    r = client.get("/health", headers={"Origin": origin})

    assert r.status_code in (200, 206, 207)
    # Starlette lowercases header keys in the client
    assert r.headers.get("access-control-allow-origin") == origin
    assert r.headers.get("access-control-allow-credentials") == "true"
