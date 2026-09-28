"""Dashboard API request bodies are bounded before any route buffers them."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

from mindroom.api import auth, main
from mindroom.api.request_body_limit import _DEFAULT_MAX_REQUEST_BODY_BYTES, install_request_body_limit

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import Message

_CHUNK = b"x" * (64 * 1024)
_SESSION_MAX_BYTES = 4 * 1024
_OPENAI_MAX_BYTES = 16 * 1024 * 1024


def _too_large(max_bytes: int) -> dict[str, str]:
    return {"detail": f"Request body exceeds {max_bytes} bytes"}


def _openai_too_large(max_bytes: int) -> dict[str, object]:
    return {
        "error": {
            "message": f"Request body exceeds {max_bytes} bytes",
            "type": "invalid_request_error",
            "param": None,
            "code": "request_too_large",
        },
    }


def _chunks_over(max_bytes: int) -> Iterator[bytes]:
    for _ in range(max_bytes // len(_CHUNK) + 1):
        yield _CHUNK


class _Payload(BaseModel):
    value: list[object]


@pytest.fixture
def limited_client() -> TestClient:
    """A minimal app with the dashboard's body limits, for streamed bodies on every route family."""
    app = FastAPI()
    install_request_body_limit(app)

    @app.post("/api/echo")
    async def echo(payload: _Payload) -> dict[str, int]:
        return {"items": len(payload.value)}

    @app.post("/v1/echo")
    async def openai_echo(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    return TestClient(app)


def test_declared_oversized_body_is_rejected_on_the_anonymous_session_route(test_client: TestClient) -> None:
    """The login route takes one API key, so a few KiB is refused before anything parses it."""
    response = test_client.post(
        "/api/auth/session",
        content=b'{"api_key": "' + b"x" * _SESSION_MAX_BYTES + b'"}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == _too_large(_SESSION_MAX_BYTES)


def test_streamed_oversized_body_without_length_is_rejected(test_client: TestClient) -> None:
    """A chunked body is cut off as soon as it crosses the limit, and the browser can still read the answer."""
    response = test_client.post(
        "/api/auth/session",
        content=_chunks_over(_SESSION_MAX_BYTES),
        headers={"Content-Type": "application/json", "Origin": "http://localhost:5173"},
    )

    assert response.status_code == 413
    assert response.json() == _too_large(_SESSION_MAX_BYTES)
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_dashboard_routes_default_to_one_mebibyte(test_client: TestClient) -> None:
    """Authenticated dashboard routes still parse their body before authentication, so their default is small."""
    response = test_client.put(
        "/api/config/raw",
        content=b" " * (_DEFAULT_MAX_REQUEST_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == _too_large(_DEFAULT_MAX_REQUEST_BODY_BYTES)


def test_streamed_json_body_above_the_default_is_rejected_before_decoding(limited_client: TestClient) -> None:
    """A streamed JSON body that would decode into millions of objects is refused while it streams."""
    body = b'{"value": [' + b"{}," * (_DEFAULT_MAX_REQUEST_BODY_BYTES // 3) + b"{}]}"

    response = limited_client.post(
        "/api/echo",
        content=iter([body[index : index + len(_CHUNK)] for index in range(0, len(body), len(_CHUNK))]),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == _too_large(_DEFAULT_MAX_REQUEST_BODY_BYTES)


def test_openai_compatible_bodies_get_openai_errors(test_client: TestClient, limited_client: TestClient) -> None:
    """`/v1` keeps a larger limit for conversation history and answers overflows in the OpenAI error shape."""
    declared = test_client.post(
        "/v1/chat/completions",
        content=b" " * (_OPENAI_MAX_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    streamed = limited_client.post("/v1/echo", content=_chunks_over(_OPENAI_MAX_BYTES))
    within = limited_client.post("/v1/echo", content=b"x" * (2 * _DEFAULT_MAX_REQUEST_BODY_BYTES))

    assert declared.status_code == 413
    assert declared.json() == _openai_too_large(_OPENAI_MAX_BYTES)
    assert streamed.status_code == 413
    assert streamed.json() == _openai_too_large(_OPENAI_MAX_BYTES)
    assert within.json() == {"bytes": 2 * _DEFAULT_MAX_REQUEST_BODY_BYTES}


def test_bodies_within_the_limit_reach_their_route(test_client: TestClient) -> None:
    """Ordinary requests are unaffected."""
    response = test_client.post("/api/auth/session", json={"api_key": "test-key"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Dashboard auth is not enabled"}


def test_knowledge_uploads_keep_their_own_larger_limit(test_client: TestClient) -> None:
    """Multipart knowledge uploads spool to disk and enforce their per-file limit, so the body limit skips them."""
    response = test_client.post(
        "/api/knowledge/bases/missing/upload",
        files={"files": ("large.txt", b"".join(_chunks_over(_DEFAULT_MAX_REQUEST_BODY_BYTES)), "text/plain")},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Knowledge base 'missing' not found"}


def test_non_multipart_bodies_on_the_upload_path_keep_the_limit(test_client: TestClient) -> None:
    """Only multipart uploads skip the limit, so a urlencoded body cannot be buffered whole on that path."""
    response = test_client.post(
        "/api/knowledge/bases/missing/upload",
        content=b"files=" + b"x" * _DEFAULT_MAX_REQUEST_BODY_BYTES,
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
