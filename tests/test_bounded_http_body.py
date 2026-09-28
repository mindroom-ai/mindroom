"""An HTTP exchange's total deadline holds even when a server trickles its headers."""

from __future__ import annotations

import socket
import threading
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.bounded_http_body import http_exchange_deadline, read_identity_body_prefix
from mindroom.custom_tools import website
from mindroom.tool_system import worker_media

if TYPE_CHECKING:
    from collections.abc import Iterator

_HEADER_TRICKLE = b"HTTP/1.1 200 OK\r\nX-Slow: "
_BODY_TRICKLE = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 1000000\r\n\r\n"
# Far longer than any deadline below, so only the total deadline can end an exchange in time.
_READ_TIMEOUT_SECONDS = 20.0
_DEADLINE_SECONDS = 0.3


@pytest.fixture
def trickle_server(request: pytest.FixtureRequest) -> Iterator[str]:
    """Serve one prefix, then one byte every 20 ms inside every read timeout, to each connection."""
    prefix: bytes = request.param
    listener = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def serve_one(connection: socket.socket) -> None:
        with connection:
            connection.recv(65536)
            try:
                connection.sendall(prefix)
                while not stop.wait(0.02):
                    connection.sendall(b"a")
            except OSError:
                return

    def accept() -> None:
        while not stop.is_set():
            try:
                connection, _address = listener.accept()
            except OSError:
                return
            threading.Thread(target=serve_one, args=(connection,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        stop.set()
        listener.close()


@pytest.mark.parametrize("trickle_server", [_HEADER_TRICKLE, _BODY_TRICKLE], indirect=True, ids=["headers", "body"])
def test_a_trickling_exchange_ends_at_its_total_deadline(trickle_server: str) -> None:
    """Neither trickled headers nor a trickled body can hold the thread past the exchange deadline."""
    started = time.monotonic()
    with (
        pytest.raises(httpx.ReadTimeout),
        http_exchange_deadline(_DEADLINE_SECONDS) as exchange,
        httpx.Client(timeout=_READ_TIMEOUT_SECONDS) as client,
        client.stream("GET", f"{trickle_server}/", extensions=exchange.extensions) as response,
    ):
        read_identity_body_prefix(response, max_bytes=10_000_000, deadline=exchange.deadline)

    assert time.monotonic() - started < _READ_TIMEOUT_SECONDS / 2


def test_an_exchange_within_its_deadline_is_untouched() -> None:
    """A response that arrives in time is read whole, and the deadline never fires afterwards."""
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=iter([b"whole"])))
    with (
        http_exchange_deadline(_DEADLINE_SECONDS) as exchange,
        httpx.Client(transport=transport) as client,
        client.stream("GET", "http://example.test/", extensions=exchange.extensions) as response,
    ):
        body = read_identity_body_prefix(response, max_bytes=100, deadline=exchange.deadline)

    time.sleep(_DEADLINE_SECONDS * 2)
    assert body.data == b"whole"
    assert not exchange.expired


@pytest.mark.parametrize("trickle_server", [_HEADER_TRICKLE], indirect=True)
@pytest.mark.parametrize("route", ["direct", "proxy"])
def test_website_hops_end_at_their_deadline_through_either_route(
    trickle_server: str,
    route: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The website reader's server-fetch transport and its proxy route both honour the hop deadline."""
    monkeypatch.setattr(website, "_PAGE_READ_SECONDS", _DEADLINE_SECONDS)
    # Loopback is refused by the real transport, so the direct route uses a plain transport for the fake server.
    monkeypatch.setattr(website, "ServerFetchHTTPTransport", httpx.HTTPTransport)
    url, proxy = (f"{trickle_server}/", None) if route == "direct" else ("http://example.test/", trickle_server)
    started = time.monotonic()

    with pytest.raises(httpx.ReadTimeout):
        website._server_fetch_get(url, timeout=int(_READ_TIMEOUT_SECONDS), proxy=proxy)

    assert time.monotonic() - started < _READ_TIMEOUT_SECONDS / 2


@pytest.mark.parametrize("trickle_server", [_HEADER_TRICKLE], indirect=True)
def test_worker_media_reads_end_at_their_deadline(trickle_server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker media URL reads honour the same whole-exchange deadline."""
    monkeypatch.setattr(worker_media, "_READ_URL_SECONDS", _DEADLINE_SECONDS)
    monkeypatch.setattr(worker_media, "ServerFetchHTTPTransport", httpx.HTTPTransport)
    monkeypatch.setattr(worker_media, "get_environment_proxies", dict)
    started = time.monotonic()

    with pytest.raises(httpx.ReadTimeout):
        worker_media._read_url(f"{trickle_server}/media", 1024)

    assert time.monotonic() - started < _READ_TIMEOUT_SECONDS / 2
