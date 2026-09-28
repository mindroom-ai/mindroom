"""Bounded reads of HTTP response bodies exactly as they arrived on the wire."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.bounded_bytes import BytePrefix, collect_sync_byte_prefix

if TYPE_CHECKING:
    import httpx

# The codings httpx decodes, plus the legacy `x-gzip` alias; any of them lets a small body stand for a huge one.
# Other values, such as `utf-8` or `none`, name no compression, so httpx and these readers ignore them.
_COMPRESSED_CONTENT_CODINGS = frozenset({"gzip", "x-gzip", "deflate", "br", "zstd"})


class CompressedHttpBodyError(ValueError):
    """A server compressed a body the client requested with identity content encoding."""


def read_identity_body_prefix(response: httpx.Response, *, max_bytes: int) -> BytePrefix:
    """Read at most ``max_bytes`` of a streamed response's raw body, refusing compressed bodies unread.

    Callers request ``Accept-Encoding: identity`` and read raw bytes, so no body is ever decompressed.
    """
    codings = {value.strip().lower() for value in response.headers.get_list("content-encoding", split_commas=True)}
    if not codings.isdisjoint(_COMPRESSED_CONTENT_CODINGS):
        msg = "The response must use identity content encoding."
        raise CompressedHttpBodyError(msg)
    return collect_sync_byte_prefix(response.iter_raw(), max_bytes=max_bytes)
