"""In-memory bounded request body reader and multipart/form-data parser."""

from __future__ import annotations

from typing import TYPE_CHECKING

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from mindroom.bounded_bytes import ByteLimitExceededError, collect_bounded_bytes

if TYPE_CHECKING:
    from fastapi import Request


class RequestBodyTooLargeError(ValueError):
    """The request body exceeds the configured byte limit."""


class InvalidMultipartFormError(ValueError):
    """The request is not a valid ``multipart/form-data`` payload."""


async def read_bounded_body(request: Request, *, max_bytes: int) -> bytes:
    """Return the request body in memory, rejecting payloads over ``max_bytes`` before or while reading."""
    declared = request.headers.get("content-length", "").strip()
    significant = declared.lstrip("0")
    too_large_message = f"The request is larger than {max_bytes:,} bytes."
    if declared.isdigit() and (len(significant) > len(str(max_bytes)) or int(significant or "0") > max_bytes):
        raise RequestBodyTooLargeError(too_large_message)
    try:
        return await collect_bounded_bytes(request.stream(), max_bytes=max_bytes)
    except ByteLimitExceededError:
        raise RequestBodyTooLargeError(too_large_message) from None


def _parse_multipart_fields(body: bytes, boundary: bytes) -> dict[str, bytes]:
    """Return each part of a multipart form by its name (first occurrence), parsed strictly in memory."""
    fields: dict[str, bytes] = {}
    header_name = bytearray()
    header_value = bytearray()
    data = bytearray()
    name: str | None = None

    def begin_part() -> None:
        nonlocal name
        name = None
        data.clear()

    def end_header() -> None:
        nonlocal name
        if bytes(header_name).strip().lower() == b"content-disposition":
            name = parse_options_header(bytes(header_value))[1].get(b"name", b"").decode("utf-8", "replace")
        header_name.clear()
        header_value.clear()

    def end_part() -> None:
        if name is not None:
            fields.setdefault(name, bytes(data))

    parser = MultipartParser(
        boundary,
        {
            "on_part_begin": begin_part,
            "on_header_field": lambda chunk, start, end: header_name.extend(chunk[start:end]),
            "on_header_value": lambda chunk, start, end: header_value.extend(chunk[start:end]),
            "on_header_end": end_header,
            "on_part_data": lambda chunk, start, end: data.extend(chunk[start:end]),
            "on_part_end": end_part,
        },
    )
    try:
        parser.write(body)
        parser.finalize()
    except MultipartParseError:
        msg = "The multipart form could not be read."
        raise InvalidMultipartFormError(msg) from None
    return fields


async def parse_multipart_form(request: Request, *, max_bytes: int) -> dict[str, bytes]:
    """Validate ``multipart/form-data``, read up to ``max_bytes`` in memory, and return its fields."""
    content_type, options = parse_options_header(request.headers.get("content-type"))
    boundary = options.get(b"boundary")
    if content_type != b"multipart/form-data" or not boundary:
        msg = "Send the request as multipart/form-data."
        raise InvalidMultipartFormError(msg)
    return _parse_multipart_fields(await read_bounded_body(request, max_bytes=max_bytes), boundary)
