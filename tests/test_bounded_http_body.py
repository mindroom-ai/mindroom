"""An HTTP exchange's total deadline holds even when a server trickles its headers."""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mindroom import bounded_http_body
from mindroom.bounded_http_body import http_exchange_deadline, read_identity_body_prefix
from mindroom.custom_tools import website
from mindroom.tool_system import worker_media

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_HEADER_TRICKLE = b"HTTP/1.1 200 OK\r\nX-Slow: "
_BODY_TRICKLE = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 1000000\r\n\r\n"
# Far longer than any deadline below, so only the total deadline can end an exchange in time.
_READ_TIMEOUT_SECONDS = 20.0
_DEADLINE_SECONDS = 0.3


type _Handler = Callable[[socket.socket, threading.Event], None]


def _trickle(connection: socket.socket, prefix: bytes, stop: threading.Event) -> None:
    """Send one prefix, then one byte every 20 ms inside every read timeout, until the read timeout has passed."""
    connection.recv(65536)
    ends = time.monotonic() + _READ_TIMEOUT_SECONDS
    connection.sendall(prefix)
    while time.monotonic() < ends and not stop.wait(0.02):
        connection.sendall(b"a")


@pytest.fixture
def serve() -> Iterator[Callable[[_Handler], str]]:
    """Start loopback servers that hand each accepted connection to a handler thread."""
    stop = threading.Event()
    listeners: list[socket.socket] = []

    def start(handle: _Handler) -> str:
        listener = socket.create_server(("127.0.0.1", 0))
        listeners.append(listener)

        def serve_one(connection: socket.socket) -> None:
            with connection:
                try:
                    handle(connection, stop)
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
        return f"127.0.0.1:{listener.getsockname()[1]}"

    try:
        yield start
    finally:
        stop.set()
        for listener in listeners:
            listener.close()


@pytest.fixture
def trickle_server(request: pytest.FixtureRequest, serve: Callable[[_Handler], str]) -> str:
    """Serve plain HTTP that trickles after one prefix to each connection."""
    prefix: bytes = request.param
    return f"http://{serve(lambda connection, stop: _trickle(connection, prefix, stop))}"


@dataclass(frozen=True)
class _Tls:
    server: ssl.SSLContext
    client: ssl.SSLContext


@pytest.fixture(scope="module")
def tls(tmp_path_factory: pytest.TempPathFactory) -> _Tls:
    """Return a server context with a fresh self-signed loopback certificate and a client context trusting it."""
    directory: Path = tmp_path_factory.mktemp("tls")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate_path, key_path)
    return _Tls(server=server, client=ssl.create_default_context(cafile=certificate_path))


@pytest.fixture
def https_trickle_client(
    request: pytest.FixtureRequest,
    serve: Callable[[_Handler], str],
    tls: _Tls,
) -> Iterator[tuple[str, httpx.Client]]:
    """Return an HTTPS URL that trickles after one prefix, and a client reaching it directly or through a proxy."""
    prefix, route = request.param

    def trickle_tls(connection: socket.socket, stop: threading.Event) -> None:
        with tls.server.wrap_socket(connection, server_side=True) as encrypted:
            _trickle(encrypted, prefix, stop)

    target = serve(trickle_tls)
    proxy = None
    if route == "proxy":

        def tunnel(connection: socket.socket, stop: threading.Event) -> None:
            received = b""
            while b"\r\n\r\n" not in received:
                received += connection.recv(65536)
            connection.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            trickle_tls(connection, stop)

        proxy = f"http://{serve(tunnel)}"
    with httpx.Client(timeout=_READ_TIMEOUT_SECONDS, verify=tls.client, proxy=proxy) as client:
        yield f"https://{target}/", client


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
        read_identity_body_prefix(response, max_bytes=10_000_000, exchange=exchange)

    assert time.monotonic() - started < _READ_TIMEOUT_SECONDS / 2


@pytest.mark.parametrize(
    "https_trickle_client",
    [
        (_HEADER_TRICKLE, "direct"),
        (_BODY_TRICKLE, "direct"),
        (_HEADER_TRICKLE, "proxy"),
        (_BODY_TRICKLE, "proxy"),
    ],
    indirect=True,
    ids=["headers-direct", "body-direct", "headers-proxy", "body-proxy"],
)
def test_a_trickling_https_exchange_ends_at_its_total_deadline(https_trickle_client: tuple[str, httpx.Client]) -> None:
    """TLS connections, direct or tunnelled through a proxy, are shut down at the exchange deadline too."""
    url, client = https_trickle_client
    started = time.monotonic()
    with (
        pytest.raises(httpx.ReadTimeout),
        http_exchange_deadline(_DEADLINE_SECONDS) as exchange,
        client.stream("GET", url, extensions=exchange.extensions) as response,
    ):
        read_identity_body_prefix(response, max_bytes=10_000_000, exchange=exchange)

    assert time.monotonic() - started < _READ_TIMEOUT_SECONDS / 2
    assert exchange._duplicates == []


def test_a_failed_socket_duplicate_closes_its_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicate descriptor whose socket object cannot be built is closed rather than leaked."""
    closed: list[int] = []
    left, right = socket.socketpair()

    def refuse(*_args: object, **_kwargs: object) -> socket.socket:
        msg = "no socket object"
        raise OSError(msg)

    def record_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    real_close = bounded_http_body.os.close
    with left, right:
        monkeypatch.setattr(bounded_http_body.socket, "socket", refuse)
        monkeypatch.setattr(bounded_http_body.os, "close", record_close)
        with pytest.raises(OSError, match="no socket object"):
            bounded_http_body._owned_duplicate(left)
        monkeypatch.undo()

    assert len(closed) == 1


def test_an_exchange_within_its_deadline_is_untouched() -> None:
    """A response that arrives in time is read whole, and the deadline never fires afterwards."""
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=iter([b"whole"])))
    with (
        http_exchange_deadline(_DEADLINE_SECONDS) as exchange,
        httpx.Client(transport=transport) as client,
        client.stream("GET", "http://example.test/", extensions=exchange.extensions) as response,
    ):
        body = read_identity_body_prefix(response, max_bytes=100, exchange=exchange)

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
    """Worker media URL reads honour the same exchange deadline."""
    monkeypatch.setattr(worker_media, "_READ_URL_SECONDS", _DEADLINE_SECONDS)
    monkeypatch.setattr(worker_media, "ServerFetchHTTPTransport", httpx.HTTPTransport)
    monkeypatch.setattr(worker_media, "get_environment_proxies", dict)
    started = time.monotonic()

    with pytest.raises(httpx.ReadTimeout):
        worker_media._read_url(f"{trickle_server}/media", 1024)

    assert time.monotonic() - started < _READ_TIMEOUT_SECONDS / 2
