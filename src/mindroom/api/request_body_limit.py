"""Request body size limit for the dashboard API, which FastAPI decodes before routes authenticate callers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024
# Knowledge uploads stream their files to disk and enforce their own per-file limit.
_KNOWLEDGE_UPLOAD_PATH = re.compile(r"/api/knowledge/bases/[^/]+/upload")
_TOO_LARGE_DETAIL = f"Request body exceeds {_MAX_REQUEST_BODY_BYTES} bytes"


class RequestBodyLimitMiddleware:
    """Answer 413 once a request body exceeds the limit, whether declared up front or streamed."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass through requests whose bodies stay within the limit."""
        if scope["type"] != "http" or _KNOWLEDGE_UPLOAD_PATH.fullmatch(scope["path"]):
            await self.app(scope, receive, send)
            return
        declared = next((value for name, value in scope["headers"] if name == b"content-length"), b"")
        if declared.isdigit() and int(declared) > _MAX_REQUEST_BODY_BYTES:
            await JSONResponse({"detail": _TOO_LARGE_DETAIL}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > _MAX_REQUEST_BODY_BYTES:
                    # FastAPI re-raises HTTPException from body reads, so the route answers 413.
                    raise HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)
            return message

        await self.app(scope, bounded_receive, send)
