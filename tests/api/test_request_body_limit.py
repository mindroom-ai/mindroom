"""Dashboard API request bodies are bounded before any route buffers them."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.api import auth, main
from mindroom.api.request_body_limit import _MAX_REQUEST_BODY_BYTES

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastapi.testclient import TestClient
    from starlette.types import Message

_CHUNK = b"x" * (1024 * 1024)
_TOO_LARGE_DETAIL = {"detail": f"Request body exceeds {_MAX_REQUEST_BODY_BYTES} bytes"}


def _oversized_chunks() -> Iterator[bytes]:
    for _ in range(_MAX_REQUEST_BODY_BYTES // len(_CHUNK) + 1):
        yield _CHUNK


def test_declared_oversized_body_is_rejected_on_unauthenticated_route(test_client: TestClient) -> None:
    """A Content-Length above the limit gets 413 before the anonymous session route parses anything."""
    response = test_client.post(
        "/api/auth/session",
        content=b"{" + b" " * _MAX_REQUEST_BODY_BYTES + b"}",
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
        content=b" " * (_MAX_REQUEST_BODY_BYTES + 1),
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
        files={"files": ("large.txt", _CHUNK * (_MAX_REQUEST_BODY_BYTES // len(_CHUNK) + 1), "text/plain")},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Knowledge base 'missing' not found"}


def test_non_multipart_bodies_on_the_upload_path_keep_the_limit(test_client: TestClient) -> None:
    """Only multipart uploads skip the limit, so a urlencoded body cannot be buffered whole on that path."""
    response = test_client.post(
        "/api/knowledge/bases/missing/upload",
        content=b"files=" + b"x" * _MAX_REQUEST_BODY_BYTES,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_knowledge_upload_authenticates_before_reading_the_body(test_client: TestClient) -> None:
    """An unauthenticated upload is refused without the route reading a single body chunk."""
    runtime_paths = main._app_runtime_paths(main.app)
    main._app_context(main.app).auth_state = auth.ApiAuthState(
        runtime_paths=runtime_paths,
        settings=auth._ApiAuthSettings(
            supabase_url=None,
            supabase_anon_key=None,
            account_id=None,
            mindroom_api_key="test-key",
        ),
        supabase_auth=None,
    )
    body_reads = 0
    statuses: list[int] = []

    async def receive() -> Message:
        nonlocal body_reads
        body_reads += 1
        return {"type": "http.request", "body": b"--upload--\r\n", "more_body": body_reads < 3}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/knowledge/bases/research/upload",
        "raw_path": b"/api/knowledge/bases/research/upload",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"localhost"), (b"content-type", b"multipart/form-data; boundary=upload")],
        "client": ("127.0.0.1", 50000),
        "server": ("localhost", 80),
    }

    await test_client.app(scope, receive, send)

    assert statuses == [401]
    assert body_reads == 0
