"""Bounded HTTP exchanges whose bodies are read exactly as they arrived on the wire."""

from __future__ import annotations

import os
import socket
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.bounded_bytes import BytePrefix, ByteStreamDeadlineError, collect_sync_byte_prefix

if TYPE_CHECKING:
    from collections.abc import Iterator

# The codings httpx decodes, plus the legacy `x-gzip` alias; any of them lets a small body stand for a huge one.
# Other values, such as `utf-8` or `none`, name no compression, so httpx and these readers ignore them.
_COMPRESSED_CONTENT_CODINGS = frozenset({"gzip", "x-gzip", "deflate", "br", "zstd"})
_CONNECTED_EVENT = "connection.connect_tcp.complete"


class CompressedHttpBodyError(ValueError):
    """A server compressed a body the client requested with identity content encoding."""


@dataclass
class HttpExchange:
    """One HTTP exchange's total deadline, headers included, and the request extensions that enforce it.

    httpx timeouts apply to each network read, so a server sending one header byte inside every read timeout
    could hold the calling thread for hours. Every connection the exchange opens is recorded through httpcore's
    ``trace`` extension, and once the deadline passes its sockets are shut down, which wakes a blocked read.
    """

    deadline: float
    expired: bool = field(default=False, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _sockets: list[socket.socket] = field(default_factory=list, init=False)

    @property
    def extensions(self) -> dict[str, Any]:
        """Return request extensions that let the exchange watch its connections."""
        return {"trace": self._trace}

    def _trace(self, event_name: str, info: dict[str, Any]) -> None:
        if event_name != _CONNECTED_EVENT:
            return
        connected = info["return_value"].get_extra_info("socket")
        if not isinstance(connected, socket.socket):
            return
        with self._lock:
            self._sockets.append(connected)
            if not self.expired:
                return
        _shut_down(connected)

    def expire(self) -> None:
        """Mark the deadline as passed and wake every read blocked on the exchange's connections."""
        with self._lock:
            self.expired = True
            connected = list(self._sockets)
        for each in connected:
            _shut_down(each)


def _shut_down(connected: socket.socket) -> None:
    # A duplicate descriptor shuts down the same connection without touching the socket object another thread
    # is reading through, which matters for TLS sockets.
    with suppress(OSError):
        descriptor = connected.fileno()
        if descriptor >= 0:
            with socket.socket(fileno=os.dup(descriptor)) as duplicate:
                duplicate.shutdown(socket.SHUT_RDWR)


@contextmanager
def http_exchange_deadline(seconds: float) -> Iterator[HttpExchange]:
    """Bound one whole HTTP exchange, redirects included, to ``seconds``, raising ``httpx.ReadTimeout`` past it.

    Pass ``exchange.extensions`` to every request and ``exchange.deadline`` to ``read_identity_body_prefix``.
    """
    exchange = HttpExchange(deadline=monotonic() + seconds)
    timer = threading.Timer(seconds, exchange.expire)
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


def read_identity_body_prefix(response: httpx.Response, *, max_bytes: int, deadline: float) -> BytePrefix:
    """Read at most ``max_bytes`` of a streamed response's raw body, refusing compressed bodies unread.

    Callers request ``Accept-Encoding: identity`` and read raw bytes, so no body is ever decompressed.
    A body still arriving at ``deadline``, a ``time.monotonic()`` instant, raises ``httpx.ReadTimeout``.
    """
    codings = {value.strip().lower() for value in response.headers.get_list("content-encoding", split_commas=True)}
    if not codings.isdisjoint(_COMPRESSED_CONTENT_CODINGS):
        msg = "The response must use identity content encoding."
        raise CompressedHttpBodyError(msg)
    try:
        return collect_sync_byte_prefix(response.iter_raw(), max_bytes=max_bytes, deadline=deadline)
    except ByteStreamDeadlineError as error:
        msg = "The response body did not arrive before its deadline."
        raise httpx.ReadTimeout(msg, request=response.request) from error
