"""Request body size limit for the dashboard API.

FastAPI buffers a request body whole before it validates, authenticates, or
rejects the request, and the dashboard shares its process with every agent.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
_TOO_LARGE_DETAIL = f"Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes"
# Multipart parsing spools file parts to disk, and knowledge uploads enforce their own per-file limit.
_UNLIMITED_BODY_PATH = re.compile(r"/api/knowledge/bases/[^/]+/upload")


def _declared_content_length(scope: Scope) -> int | None:
    for name, value in scope["headers"]:
        if name == b"content-length":
            return int(value) if value.isdigit() else None
    return None


class RequestBodyLimitMiddleware:
    """Answer 413 once a request body crosses the limit, whether declared up front or streamed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass through requests whose bodies stay within the limit."""
        if scope["type"] != "http" or _UNLIMITED_BODY_PATH.fullmatch(scope["path"]):
            await self.app(scope, receive, send)
            return
        declared_length = _declared_content_length(scope)
        if declared_length is not None and declared_length > MAX_REQUEST_BODY_BYTES:
            await JSONResponse({"detail": _TOO_LARGE_DETAIL}, status_code=413)(scope, receive, send)
            return

        received_bytes = 0

        async def bounded_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > MAX_REQUEST_BODY_BYTES:
                    # FastAPI re-raises HTTPException from body reads, so routes answer 413 instead of 400.
                    raise HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)
            return message

        await self.app(scope, bounded_receive, send)
