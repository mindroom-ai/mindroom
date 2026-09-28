"""Dashboard API request bodies are bounded before any route buffers them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.api.request_body_limit import MAX_REQUEST_BODY_BYTES

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient

_CHUNK = b"x" * (1024 * 1024)
_TOO_LARGE_DETAIL = {"detail": f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes"}


def _oversized_chunks() -> Iterator[bytes]:
    for _ in range(MAX_REQUEST_BODY_BYTES // len(_CHUNK) + 1):
        yield _CHUNK


def test_declared_oversized_body_is_rejected_on_unauthenticated_route(test_client: TestClient) -> None:
    """A Content-Length above the limit gets 413 before the anonymous session route parses anything."""
    response = test_client.post(
        "/api/auth/session",
        content=b"{" + b" " * MAX_REQUEST_BODY_BYTES + b"}",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == _TOO_LARGE_DETAIL


def test_streamed_oversized_body_without_length_is_rejected(test_client: TestClient) -> None:
    """A chunked body is cut off as soon as it crosses the limit, and the browser can still read the answer."""
    response = test_client.post(
        "/api/auth/session",
        content=_oversized_chunks(),
        headers={"Content-Type": "application/json", "Origin": "http://localhost:5173"},
    )

    assert response.status_code == 413
    assert response.json() == _TOO_LARGE_DETAIL
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_openai_compatible_body_above_the_limit_is_rejected(test_client: TestClient) -> None:
    """The OpenAI-compatible endpoint shares the same limit."""
    response = test_client.post(
        "/v1/chat/completions",
        content=b" " * (MAX_REQUEST_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413


def test_bodies_within_the_limit_reach_their_route(test_client: TestClient) -> None:
    """Ordinary requests are unaffected."""
    response = test_client.post("/api/auth/session", json={"api_key": "test-key"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Dashboard auth is not enabled"}


def test_knowledge_uploads_keep_their_own_larger_limit(test_client: TestClient) -> None:
    """Multipart knowledge uploads spool to disk and enforce their per-file limit, so the body limit skips them."""
    response = test_client.post(
        "/api/knowledge/bases/missing/upload",
        files={"files": ("large.txt", _CHUNK * (MAX_REQUEST_BODY_BYTES // len(_CHUNK) + 1), "text/plain")},
    )

    assert response.status_code != 413
