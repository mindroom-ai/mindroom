"""Request body size limits for the dashboard API.

FastAPI reads and decodes a declared request body before route dependencies authenticate the caller,
and the dashboard shares its process with every agent, so every route's body is limited up front.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from mindroom.api.openai_streaming_protocol import error_response

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_DEFAULT_MAX_REQUEST_BODY_BYTES = 1024 * 1024
_OPENAI_COMPATIBLE_PATH = re.compile(r"/v1/.*")


@dataclass(frozen=True, slots=True)
class _PathBodyLimit:
    path: re.Pattern[str]
    max_bytes: int


_PATH_BODY_LIMITS = (
    # The anonymous login route takes one API key, and decoding JSON costs many times its size in memory.
    _PathBodyLimit(re.compile(r"/api/auth/session"), 4 * 1024),
    # OpenAI-compatible clients resend whole conversations, and the handler authenticates before reading the body.
    _PathBodyLimit(_OPENAI_COMPATIBLE_PATH, 16 * 1024 * 1024),
)
# Knowledge uploads authenticate before reading the form, accept only file parts, which spool to disk,
# and enforce their own per-file limit.
_UNLIMITED_MULTIPART_PATH = re.compile(r"/api/knowledge/bases/[^/]+/upload")


class _RequestBodyTooLargeError(HTTPException):
    """A streamed request body crossed its path's limit while a route was reading it."""

    def __init__(self, max_bytes: int) -> None:
        super().__init__(status_code=413, detail=_too_large_detail(max_bytes))


def _too_large_detail(max_bytes: int) -> str:
    return f"Request body exceeds {max_bytes} bytes"


def _too_large_response(path: str, max_bytes: int) -> JSONResponse:
    if _OPENAI_COMPATIBLE_PATH.fullmatch(path):
        return error_response(413, _too_large_detail(max_bytes), code="request_too_large")
    return JSONResponse({"detail": _too_large_detail(max_bytes)}, status_code=413)


async def _handle_request_body_too_large(request: Request, _exc: Exception) -> JSONResponse:
    path = request.scope["path"]
    return _too_large_response(path, _max_body_bytes(path))


def _header(scope: Scope, name: bytes) -> bytes | None:
    return next((value for header_name, value in scope["headers"] if header_name == name), None)


def _is_unlimited_upload(scope: Scope) -> bool:
    content_type = _header(scope, b"content-type") or b""
    return (
        _UNLIMITED_MULTIPART_PATH.fullmatch(scope["path"]) is not None
        and content_type.split(b";", 1)[0].strip().lower() == b"multipart/form-data"
    )


def _max_body_bytes(path: str) -> int:
    return next(
        (limit.max_bytes for limit in _PATH_BODY_LIMITS if limit.path.fullmatch(path)),
        _DEFAULT_MAX_REQUEST_BODY_BYTES,
    )


def _declared_content_length(scope: Scope) -> int | None:
    value = _header(scope, b"content-length")
    return int(value) if value is not None and value.isdigit() else None


class _RequestBodyLimitMiddleware:
    """Answer 413 once a request body crosses its path's limit, whether declared up front or streamed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass through requests whose bodies stay within their path's limit."""
        if scope["type"] != "http" or _is_unlimited_upload(scope):
            await self.app(scope, receive, send)
            return
        max_bytes = _max_body_bytes(scope["path"])
        declared_length = _declared_content_length(scope)
        if declared_length is not None and declared_length > max_bytes:
            await _too_large_response(scope["path"], max_bytes)(scope, receive, send)
            return

        received_bytes = 0

        async def bounded_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > max_bytes:
                    # FastAPI re-raises HTTPException from body reads, so routes answer 413 instead of 400.
                    raise _RequestBodyTooLargeError(max_bytes)
            return message

        await self.app(scope, bounded_receive, send)


def install_request_body_limit(app: FastAPI) -> None:
    """Limit every request body the app reads and answer overflows in each route family's error shape."""
    app.add_exception_handler(_RequestBodyTooLargeError, _handle_request_body_too_large)
    app.add_middleware(_RequestBodyLimitMiddleware)
