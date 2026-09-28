"""Bounded reads of HTTP response bodies exactly as they arrived on the wire."""

from __future__ import annotations

from time import monotonic

import httpx

from mindroom.bounded_bytes import BytePrefix, ByteStreamDeadlineError, collect_sync_byte_prefix

# The codings httpx decodes, plus the legacy `x-gzip` alias; any of them lets a small body stand for a huge one.
# Other values, such as `utf-8` or `none`, name no compression, so httpx and these readers ignore them.
_COMPRESSED_CONTENT_CODINGS = frozenset({"gzip", "x-gzip", "deflate", "br", "zstd"})


class CompressedHttpBodyError(ValueError):
    """A server compressed a body the client requested with identity content encoding."""


def read_identity_body_prefix(response: httpx.Response, *, max_bytes: int, timeout_seconds: float) -> BytePrefix:
    """Read at most ``max_bytes`` of a streamed response's raw body, refusing compressed bodies unread.

    Callers request ``Accept-Encoding: identity`` and read raw bytes, so no body is ever decompressed.
    httpx timeouts apply to each read, so the whole body must also arrive within ``timeout_seconds``;
    otherwise this raises ``httpx.ReadTimeout``.
    """
    codings = {value.strip().lower() for value in response.headers.get_list("content-encoding", split_commas=True)}
    if not codings.isdisjoint(_COMPRESSED_CONTENT_CODINGS):
        msg = "The response must use identity content encoding."
        raise CompressedHttpBodyError(msg)
    try:
        return collect_sync_byte_prefix(
            response.iter_raw(),
            max_bytes=max_bytes,
            deadline=monotonic() + timeout_seconds,
        )
    except ByteStreamDeadlineError as error:
        msg = f"The response body did not arrive within {timeout_seconds:g} seconds."
        raise httpx.ReadTimeout(msg, request=response.request) from error
