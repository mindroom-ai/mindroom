"""The guarded public URL fetch that core tools and plugins share."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import TYPE_CHECKING

import httpcore
import httpx
import pytest

from mindroom import public_fetch
from mindroom.public_fetch import PublicFetchError, PublicFetchResult, fetch_public_url, fetch_public_url_sync

if TYPE_CHECKING:
    from collections.abc import Callable

_PUBLIC_ADDRESS = "93.184.216.34"
_PROXY_HOST = "10.0.0.10"


def _page(status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> httpx.Response:
    """A response whose body streams like one off the network, so the fetch reads it raw."""
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(body))


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Answer every request that passed MindRoom's transports, and record it."""
    requests: list[httpx.Request] = []

    async def respond(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    return requests


def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(host: str, port: int, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        del host
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_PUBLIC_ADDRESS, port))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


async def _refusal(url: str, *, max_bytes: int = 1024) -> PublicFetchError:
    with pytest.raises(PublicFetchError) as exc_info:
        await fetch_public_url(url, max_bytes=max_bytes)
    return exc_info.value


@pytest.mark.asyncio
async def test_a_fetch_follows_public_redirects_and_returns_the_final_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relative and cross-host redirects are followed, and the result names the page they ended on."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/moved"})
        if request.url.path == "/moved":
            return httpx.Response(301, headers={"Location": "https://www.example.com/final"})
        return _page(200, b"<p>page</p>", {"Content-Type": "text/html; charset=utf-8"})

    requests = _serve(monkeypatch, handler)

    result = await fetch_public_url(" https://example.com/start ", max_bytes=1024)

    assert result == PublicFetchResult(
        url="https://www.example.com/final",
        content_type="text/html; charset=utf-8",
        content=b"<p>page</p>",
    )
    assert [str(request.url) for request in requests] == [
        "https://example.com/start",
        "https://example.com/moved",
        "https://www.example.com/final",
    ]
    assert [request.headers["accept-encoding"] for request in requests] == ["identity"] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://localhost/",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http://[::1",
    ],
)
async def test_a_url_that_is_not_public_http_is_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    """Loopback, private, link-local, local names, other schemes, and malformed URLs never reach the network."""
    requests = _serve(monkeypatch, lambda _request: _page(200, b"secret"))

    error = await _refusal(url)

    assert error.code == "url_not_allowed"
    assert requests == []


@pytest.mark.asyncio
async def test_a_redirect_to_loopback_is_refused_before_it_is_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public page cannot hand the fetch to a loopback service."""
    requests = _serve(monkeypatch, lambda _request: httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"}))

    error = await _refusal("https://example.com/redirect")

    assert error.code == "url_not_allowed"
    assert "private_address" in str(error)
    assert [str(request.url) for request in requests] == ["https://example.com/redirect"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "requested"),
    [
        ("https://user:secret@example.com/", []),
        ("https://example.com/redirect", ["https://example.com/redirect"]),
    ],
)
async def test_credentials_in_a_url_or_a_redirect_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
    requested: list[str],
) -> None:
    """A fetch sends no credentials, so a URL carrying them is refused instead of sent."""
    requests = _serve(
        monkeypatch,
        lambda _request: httpx.Response(302, headers={"Location": "https://user:secret@example.com/next"}),
    )

    error = await _refusal(url)

    assert error.code == "url_not_allowed"
    assert [str(request.url) for request in requests] == requested


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["gzip", "br", "identity, gzip", "unknown"])
async def test_a_body_with_a_content_encoding_is_refused(monkeypatch: pytest.MonkeyPatch, encoding: str) -> None:
    """Only identity bodies come back, so no caller receives bytes it would have to decode."""
    _serve(monkeypatch, lambda _request: _page(200, b"x", {"Content-Encoding": encoding}))

    error = await _refusal("https://example.com/")

    assert error.code == "content_encoding"


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["identity", "Identity, identity"])
async def test_an_identity_body_is_returned(monkeypatch: pytest.MonkeyPatch, encoding: str) -> None:
    """An explicit identity coding is the plain body."""
    _serve(monkeypatch, lambda _request: _page(200, b"plain", {"Content-Encoding": encoding}))

    result = await fetch_public_url("https://example.com/", max_bytes=1024)

    assert result.content == b"plain"


@pytest.mark.asyncio
async def test_a_body_within_the_limit_is_returned_and_a_larger_one_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The byte limit is inclusive and bounds the raw body."""
    bodies = {"/five": b"12345", "/six": b"123456"}
    _serve(monkeypatch, lambda request: _page(200, bodies[request.url.path]))

    assert (await fetch_public_url("https://example.com/five", max_bytes=5)).content == b"12345"
    error = await _refusal("https://example.com/six", max_bytes=5)

    assert error.code == "too_large"


@pytest.mark.asyncio
@pytest.mark.parametrize(("redirects", "succeeds"), [(10, True), (11, False)])
async def test_ten_redirects_are_followed_and_an_eleventh_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    redirects: int,
    succeeds: bool,
) -> None:
    """A redirect chain stops after ten hops without sending a twelfth request."""

    def handler(request: httpx.Request) -> httpx.Response:
        hop = int(request.url.path.removeprefix("/hop-"))
        if hop < redirects:
            return httpx.Response(302, headers={"Location": f"/hop-{hop + 1}"})
        return _page(200, b"end")

    requests = _serve(monkeypatch, handler)

    if succeeds:
        assert (await fetch_public_url("https://example.com/hop-0", max_bytes=1024)).content == b"end"
    else:
        assert (await _refusal("https://example.com/hop-0")).code == "too_many_redirects"
    assert len(requests) == 11


@pytest.mark.asyncio
async def test_an_error_status_is_refused_with_its_status_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page the server does not serve is a failure the caller can tell apart by status."""
    _serve(monkeypatch, lambda _request: _page(404, b"missing"))

    error = await _refusal("https://example.com/missing")

    assert (error.code, error.status_code) == ("http_status", 404)


@pytest.mark.asyncio
async def test_a_connection_failure_is_a_request_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Network errors keep their own code, apart from refusals."""

    def refuse_connection(request: httpx.Request) -> httpx.Response:
        msg = "connection refused"
        raise httpx.ConnectError(msg, request=request)

    _serve(monkeypatch, refuse_connection)

    error = await _refusal("https://example.com/")

    assert error.code == "request_failed"


class _TrickleStream(httpcore.AsyncMockStream):
    """A server that sends its headers, or its headers and then its body, one byte at a time."""

    def __init__(self, prefix: bytes) -> None:
        super().__init__([])
        self._prefix = prefix

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        del max_bytes, timeout
        await asyncio.sleep(0.02)
        if self._prefix:
            chunk, self._prefix = self._prefix[:1], self._prefix[1:]
            return chunk
        return b"x"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prefix",
    [b"HTTP/1.1 200 OK\r\nX-Slow: ", b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n"],
    ids=["headers", "body"],
)
async def test_a_server_that_trickles_bytes_fails_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
    prefix: bytes,
) -> None:
    """One deadline bounds the whole fetch, however steadily a slow server keeps sending."""
    monkeypatch.setattr(public_fetch, "_DEADLINE_SECONDS", 0.3)
    _public_dns(monkeypatch)

    async def connect(_backend: httpcore.AnyIOBackend, *_args: object, **_kwargs: object) -> _TrickleStream:
        return _TrickleStream(prefix)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    started = time.monotonic()

    error = await _refusal("http://slow.example/", max_bytes=1_000_000)

    assert error.code == "timeout"
    assert time.monotonic() - started < 2


def test_a_synchronous_fetch_returns_at_the_deadline_while_a_lookup_hangs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A hostname lookup that outlives the deadline does not hold a synchronous caller."""
    monkeypatch.setattr(public_fetch, "_DEADLINE_SECONDS", 0.3)
    released = threading.Event()
    real_getaddrinfo = socket.getaddrinfo

    def hang(*args: object, **kwargs: object) -> object:
        released.wait(10)
        return real_getaddrinfo(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", hang)
    started = time.monotonic()
    try:
        with pytest.raises(PublicFetchError) as exc_info:
            fetch_public_url_sync("https://hanging-lookup.example/", max_bytes=1024)
        elapsed = time.monotonic() - started
    finally:
        released.set()

    assert exc_info.value.code == "timeout"
    assert elapsed < 2


class _RecordingStream(httpcore.AsyncMockStream):
    def __init__(self, responses: list[bytes]) -> None:
        super().__init__(responses)
        self.written = bytearray()

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        del timeout
        self.written.extend(buffer)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("use_env_proxy", "dialed", "request_line"),
    [
        (False, (_PUBLIC_ADDRESS, 80), b"GET /page HTTP/1.1\r\n"),
        (True, (_PROXY_HOST, 3128), b"GET http://media.example/page HTTP/1.1\r\n"),
    ],
)
async def test_the_environment_proxy_carries_a_fetch_only_when_asked(
    monkeypatch: pytest.MonkeyPatch,
    use_env_proxy: bool,
    dialed: tuple[str, int],
    request_line: bytes,
) -> None:
    """Without the flag a fetch dials the address it validated, even when a proxy is configured."""
    monkeypatch.setenv("HTTP_PROXY", f"http://{_PROXY_HOST}:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    _public_dns(monkeypatch)
    streams: list[tuple[tuple[str, int], _RecordingStream]] = []

    async def connect(_backend: httpcore.AnyIOBackend, host: str, port: int, **_kwargs: object) -> _RecordingStream:
        stream = _RecordingStream([b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\npage"])
        streams.append(((host, port), stream))
        return stream

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)

    result = await fetch_public_url("http://media.example/page", max_bytes=1024, use_env_proxy=use_env_proxy)

    assert result.content == b"page"
    assert [address for address, _stream in streams] == [dialed]
    assert streams[0][1].written.startswith(request_line)
