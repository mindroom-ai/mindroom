"""Dashboard API request bodies are limited before any route reads them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.api import request_body_limit

if TYPE_CHECKING:
    import pytest
    from fastapi.testclient import TestClient


def test_request_bodies_over_the_limit_are_refused_before_routes_read_them(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declared and streamed bodies over the limit get 413, while knowledge uploads keep their own limit."""
    monkeypatch.setattr(request_body_limit, "_MAX_REQUEST_BODY_BYTES", 1024)
    json_headers = {"content-type": "application/json"}

    declared = test_client.post("/api/auth/session", content=b"{" + b" " * 2048 + b"}", headers=json_headers)
    huge_declared = test_client.post(
        "/api/auth/session",
        content=b"{}",
        headers={**json_headers, "content-length": "9" * 5000},
    )
    streamed = test_client.post("/api/auth/session", content=iter([b" " * 600] * 4), headers=json_headers)
    within = test_client.post("/api/auth/session", json={"api_key": "key"})
    upload = test_client.post(
        "/api/knowledge/bases/docs/upload",
        files={"files": ("notes.txt", b"x" * 4096, "text/plain")},
    )

    assert (declared.status_code, huge_declared.status_code, streamed.status_code) == (413, 413, 413)
    assert declared.json() == {"detail": request_body_limit._TOO_LARGE_DETAIL}
    # Both reach their routes, which then refuse them for unrelated reasons in this test runtime.
    assert within.json() == {"detail": "Dashboard auth is not enabled"}
    assert upload.json() == {"detail": "Knowledge base 'docs' not found"}
