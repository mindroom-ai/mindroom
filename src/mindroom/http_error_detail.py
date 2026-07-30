"""Safe error-detail extraction from HTTP error responses."""

from __future__ import annotations

from typing import Protocol

_MAX_LENGTH = 300


class _ErrorResponse(Protocol):
    """The error-body surface of an httpx.Response (a protocol so importers can lazy-import httpx)."""

    text: str

    def json(self) -> object: ...


def error_detail_from_response(response: _ErrorResponse) -> str:
    """Extract a compact, safe error detail from a JSON or plaintext error response.

    FastAPI validation errors carry ``detail`` as a list whose items echo the
    submitted request body under ``input``; only ``loc``/``msg`` are kept so
    secrets in the request body never reach error messages or logs. The raw
    body text is used only when the body is not JSON at all.
    """
    try:
        raw = response.json()
    except ValueError:
        return response.text.strip()[:_MAX_LENGTH] or "unknown error"
    body = _json_object(raw)
    detail = body.get("detail") if body is not None else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:_MAX_LENGTH]
    if isinstance(detail, list):
        parts = [part for part in map(_validation_error_part, detail) if part is not None]
        if parts:
            return "; ".join(parts)[:_MAX_LENGTH]
    return "unknown error"


def _json_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _validation_error_part(item: object) -> str | None:
    entry = _json_object(item)
    if entry is None:
        return None
    loc = entry.get("loc")
    msg = entry.get("msg")
    field = ".".join(str(part) for part in loc) if isinstance(loc, list) and loc else "request"
    return f"{field}: {msg}" if isinstance(msg, str) else field
