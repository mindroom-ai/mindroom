"""Bounded HTTP exchanges whose bodies are read exactly as they arrived on the wire."""

from __future__ import annotations

import os
import socket
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any

import httpcore
import httpx

from mindroom.bounded_bytes import BytePrefix, ByteStreamDeadlineError, collect_sync_byte_prefix

if TYPE_CHECKING:
    from collections.abc import Iterator

# The codings httpx decodes, plus the legacy `x-gzip` alias; any of them lets a small body stand for a huge one.
# Other values, such as `utf-8` or `none`, name no compression, so httpx and these readers ignore them.
_COMPRESSED_CONTENT_CODINGS = frozenset({"gzip", "x-gzip", "deflate", "br", "zstd"})
# httpcore traces `connection.connect_tcp.complete` for direct and HTTP-proxy connections and
# `socks.connect_tcp.complete` for SOCKS connections, each before any TLS handshake.
_CONNECTED_EVENT_SUFFIX = ".connect_tcp.complete"


class CompressedHttpBodyError(ValueError):
    """A server compressed a body the client requested with identity content encoding."""


@dataclass
class HttpExchange:
    """One HTTP exchange's deadline for its reads, headers and body, and the request extensions that enforce it.

    httpx timeouts apply to each network read, so a server sending one header byte inside every read timeout
    could hold the calling thread for hours. httpcore's ``trace`` extension reports each TCP connection the
    exchange opens, and the exchange immediately takes its own duplicate descriptor of that socket, before a TLS
    handshake can wrap and detach it. Once the deadline passes, shutting those duplicates down wakes any read
    blocked on the connections, plain or TLS.

    The deadline starts before name resolution and connection setup, but a blocked resolution or connect attempt
    is bounded only by the client's connect timeout for each resolved address, and a connection that completes
    after the deadline is shut down at once.
    Each exchange must use its own ``httpx.Client``: connections reused from a shared pool are never reported to
    it, and shutting them down would break other requests.
    """

    deadline: float
    expired: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _duplicates: list[socket.socket] = field(default_factory=list, init=False)

    @property
    def extensions(self) -> dict[str, Any]:
        """Return request extensions that let the exchange watch its connections."""
        return {"trace": self._trace}

    def _trace(self, event_name: str, info: dict[str, Any]) -> None:
        if not event_name.endswith(_CONNECTED_EVENT_SUFFIX):
            return
        stream = info["return_value"]
        connected = stream.get_extra_info("socket")
        if not isinstance(connected, socket.socket):
            return
        try:
            duplicate = _owned_duplicate(connected)
        except OSError as error:
            # A connection the deadline cannot watch is refused rather than left unbounded.
            stream.close()
            msg = "The HTTP exchange could not watch its connection."
            raise httpcore.ConnectError(msg) from error
        with self._lock:
            self._duplicates.append(duplicate)
            if not self.expired:
                return
        _shut_down(duplicate)

    def _expire(self) -> None:
        """Mark the deadline as passed and wake every read blocked on the exchange's connections."""
        with self._lock:
            self.expired = True
            duplicates = list(self._duplicates)
        for duplicate in duplicates:
            _shut_down(duplicate)

    def _close(self) -> None:
        with self._lock:
            duplicates, self._duplicates = self._duplicates, []
        for duplicate in duplicates:
            duplicate.close()


def _owned_duplicate(connected: socket.socket) -> socket.socket:
    descriptor = os.dup(connected.fileno())
    try:
        return socket.socket(fileno=descriptor)
    except BaseException:
        os.close(descriptor)
        raise


def _shut_down(duplicate: socket.socket) -> None:
    # Shutting down one descriptor ends the connection for every descriptor that shares it.
    with suppress(OSError):
        duplicate.shutdown(socket.SHUT_RDWR)


@contextmanager
def http_exchange_deadline(seconds: float) -> Iterator[HttpExchange]:
    """Bound one HTTP exchange's reads, redirects included, to ``seconds``, raising ``httpx.ReadTimeout`` past it.

    Pass ``exchange.extensions`` to every request of one exchange-owned client, and the exchange to
    ``read_identity_body_prefix``.
    """
    exchange = HttpExchange(deadline=monotonic() + seconds)
    timer = threading.Timer(seconds, exchange._expire)
    timer.daemon = True
    timer.start()
    try:
        yield exchange
    except httpx.TransportError as error:
        if not exchange.expired or isinstance(error, httpx.TimeoutException):
            raise
        msg = f"The HTTP exchange did not finish within {seconds:g} seconds."
        raise httpx.ReadTimeout(msg) from error
    finally:
        timer.cancel()
        timer.join()
        exchange._close()


def read_identity_body_prefix(response: httpx.Response, *, max_bytes: int, exchange: HttpExchange) -> BytePrefix:
    """Read at most ``max_bytes`` of a streamed response's raw body, refusing compressed bodies unread.

    Callers request ``Accept-Encoding: identity`` and read raw bytes, so no body is ever decompressed.
    A body still arriving at the exchange's deadline raises ``httpx.ReadTimeout``.
    """
    codings = {value.strip().lower() for value in response.headers.get_list("content-encoding", split_commas=True)}
    if not codings.isdisjoint(_COMPRESSED_CONTENT_CODINGS):
        msg = "The response must use identity content encoding."
        raise CompressedHttpBodyError(msg)
    try:
        return collect_sync_byte_prefix(response.iter_raw(), max_bytes=max_bytes, deadline=exchange.deadline)
    except ByteStreamDeadlineError as error:
        msg = "The response body did not arrive before its deadline."
        raise httpx.ReadTimeout(msg, request=response.request) from error
