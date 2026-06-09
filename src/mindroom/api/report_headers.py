"""Shared response headers for rendered report artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.responses import FileResponse

_REPORT_CSP = (
    "default-src 'none'; "
    "img-src 'self' data: https:; "
    "style-src 'unsafe-inline'; "
    "font-src 'self' data:; "
    "base-uri 'none'; "
    "frame-ancestors 'self'"
)


def set_report_headers(response: FileResponse, *, cache_control: str) -> None:
    """Set conservative browser headers for rendered report artifacts."""
    response.headers["Content-Security-Policy"] = _REPORT_CSP
    response.headers["Cache-Control"] = cache_control
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
