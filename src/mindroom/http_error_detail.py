"""Safe error-detail extraction from HTTP error responses."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

_MAX_LENGTH = 300


class _ErrorResponse(Protocol):
    """The error-body surface of an httpx.Response (a protocol so importers can lazy-import httpx)."""

    text: str

    def json(self) -> object: ...


def error_detail_from_response(response: _ErrorResponse, *, fallback: str = "unknown error") -> str:
    """Extract a compact, safe error detail from a JSON or plaintext error response.

    FastAPI validation errors carry ``detail`` as a list whose items echo the
    submitted request body under ``input``; only ``loc``/``msg`` are kept so
    secrets in the request body never reach error messages or logs. The raw
    body text is used only when the body is not JSON at all.
    ``fallback`` is returned when the response carries no usable detail.
    """
    try:
        raw = response.json()
    except ValueError:
        return response.text.strip()[:_MAX_LENGTH] or fallback
    body = _json_object(raw)
    detail = body.get("detail") if body is not None else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:_MAX_LENGTH]
    if isinstance(detail, list):
        return validation_error_detail(detail, fallback=fallback)
    return fallback


def validation_error_detail(errors: Iterable[object], *, fallback: str) -> str:
    """Name each rejected field and why, never its value, from FastAPI or pydantic validation errors."""
    parts = [part for part in map(_validation_error_part, errors) if part is not None]
    return "; ".join(parts)[:_MAX_LENGTH] if parts else fallback


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
    field = ".".join(str(part) for part in loc) if isinstance(loc, list | tuple) and loc else "request"
    return f"{field}: {msg}" if isinstance(msg, str) else field
