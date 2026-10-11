"""Worker media keeps configured proxy routing and public destination checks."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpcore
import pytest
from agno.media import File
from agno.tools.function import ToolResult

from mindroom.tool_system import media_transport
from mindroom.tool_system.media_transport import decode_media_result
from mindroom.tool_system.worker_media import serialize_worker_tool_result

if TYPE_CHECKING:
    import ssl

_CONNECT_OK = b"HTTP/1.1 200 Connection established\r\n\r\n"


class _RecordingStream(httpcore.MockStream):
    """Record HTTP bytes and TLS authority below the real HTTPX transports."""

    def __init__(self, responses: list[bytes]) -> None:
        super().__init__(responses)
        self.written = bytearray()
        self.tls_hosts: list[str | None] = []
        self.closed = False

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        """Keep wire requests while letting httpcore own HTTP framing."""
        del timeout
        self.written.extend(buffer)

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        """Capture the authority selected by the real CONNECT/TLS implementation."""
        del ssl_context, timeout
        self.tls_hosts.append(server_hostname)
        return self

    def close(self) -> None:
        """Expose stream cleanup after successful and rejected responses."""
        self.closed = True
        super().close()


@dataclass(frozen=True)
class _Connection:
    host: str
    port: int
    stream: _RecordingStream


@pytest.fixture(autouse=True)
def media_proxy_env(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Isolate proxy settings and DNS without replacing HTTPX's route selection."""
    for name in tuple(os.environ):
        if name.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            monkeypatch.delenv(name)
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    queries: list[str] = []

    def resolve(
        host: str,
        port: int,
        *_args: object,
        **_kwargs: object,
    ) -> list[tuple[int, int, int, str, tuple[str, int]]]:
        queries.append(host)
        addresses = {
            "media.example": ["93.184.216.34"],
            "cdn.example": ["93.184.216.35"],
            "private.example": ["10.0.0.8"],
            "mixed.example": ["93.184.216.34", "10.0.0.8"],
        }[host]
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port)) for address in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    return queries


def _response(
    status: str = "200 OK",
    *,
    body: bytes = b"media",
    location: str | None = None,
) -> bytes:
    headers = [
        f"HTTP/1.1 {status}",
        f"Content-Length: {len(body)}",
        "Content-Type: text/plain",
        "Connection: close",
    ]
    if location is not None:
        headers.append(f"Location: {location}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body


def _capture_connections(
    monkeypatch: pytest.MonkeyPatch,
    responses: dict[str, list[bytes]],
) -> list[_Connection]:
    connections: list[_Connection] = []

    def connect(
        _backend: httpcore.SyncBackend,
        host: str,
        port: int,
        **_kwargs: object,
    ) -> httpcore.NetworkStream:
        assert host in responses, f"Unexpected connection to {host}; direct fallback is forbidden"
        stream = _RecordingStream(list(responses[host]))
        connections.append(_Connection(host, port, stream))
        return stream

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    return connections


def _inline_file(url: str) -> File:
    result = ToolResult(content="Media", files=[File(url=url)])
    decoded = decode_media_result(json.loads(json.dumps(serialize_worker_tool_result(result))))
    assert isinstance(decoded, ToolResult)
    assert decoded.files
    item = decoded.files[0]
    assert item.url is None
    assert item.filepath is None
    assert item.media_reference is None
    assert item.external is None
    return item


@pytest.mark.parametrize(
    ("env", "scheme", "host", "port"),
    [
        ({"HTTP_PROXY": "http://10.0.0.10:3128"}, "http", "10.0.0.10", 3128),
        ({"HTTPS_PROXY": "http://10.0.0.11:3129"}, "https", "10.0.0.11", 3129),
        (
            {
                "HTTPS_PROXY": "http://10.0.0.11:3129",
                "https_proxy": "http://10.0.0.12:3130",
                "ALL_PROXY": "http://10.0.0.13:3131",
            },
            "https",
            "10.0.0.12",
            3130,
        ),
        ({"ALL_PROXY": "http://10.0.0.13:3131"}, "http", "10.0.0.13", 3131),
        ({"all_proxy": "http://10.0.0.13:3131"}, "https", "10.0.0.13", 3131),
    ],
)
def test_worker_media_uses_environment_proxy(
    monkeypatch: pytest.MonkeyPatch,
    media_proxy_env: list[str],
    env: dict[str, str],
    scheme: str,
    host: str,
    port: int,
) -> None:
    """Proxy-only workers download media without losing hostname grants or TLS."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    responses = [_CONNECT_OK, _response()] if scheme == "https" else [_response()]
    connections = _capture_connections(monkeypatch, {host: responses})

    assert _inline_file(f"{scheme}://media.example/media.txt").content == b"media"

    assert [(item.host, item.port) for item in connections] == [(host, port)]
    stream = connections[0].stream
    if scheme == "https":
        assert stream.written.startswith(b"CONNECT media.example:443 HTTP/1.1\r\n")
        assert b"GET /media.txt HTTP/1.1\r\n" in stream.written
        assert stream.tls_hosts == ["media.example"]
    else:
        assert stream.written.startswith(b"GET http://media.example/media.txt HTTP/1.1\r\n")
        assert stream.tls_hosts == []
    assert b"host: media.example\r\n" in stream.written.lower()
    assert media_proxy_env == ["media.example"]
    assert stream.closed


@pytest.mark.parametrize(
    ("name", "value"),
    [(None, None), ("NO_PROXY", "media.example"), ("no_proxy", ".example"), ("NO_PROXY", "*")],
)
def test_worker_media_direct_routes_keep_validated_address(
    monkeypatch: pytest.MonkeyPatch,
    media_proxy_env: list[str],
    name: str | None,
    value: str | None,
) -> None:
    """Absent proxies and NO_PROXY both retain the guarded direct dial."""
    if name is not None:
        assert value is not None
        monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.10:3128")
        monkeypatch.setenv(name, value)
    connections = _capture_connections(monkeypatch, {"93.184.216.34": [_response()]})

    assert _inline_file("http://media.example/media.txt").content == b"media"

    assert [(item.host, item.port) for item in connections] == [("93.184.216.34", 80)]
    assert connections[0].stream.written.startswith(b"GET /media.txt HTTP/1.1\r\n")
    assert media_proxy_env == ["media.example"]


@pytest.mark.parametrize(
    "url",
    [
        "file:///private/media.txt",
        "http://127.0.0.1/media.txt",
        "http://169.254.169.254/latest",
        "http://metadata.google.internal/media.txt",
        "http://private.example/media.txt",
        "http://mixed.example/media.txt",
    ],
)
def test_worker_media_proxy_rejects_forbidden_destinations(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    """Configured private proxy must not grant access to private media targets."""
    monkeypatch.setenv("ALL_PROXY", "http://10.0.0.10:3128")
    connections = _capture_connections(monkeypatch, {})

    with pytest.raises(ValueError, match="Unable to read worker media resource"):
        _inline_file(url)

    assert connections == []


def test_worker_media_no_proxy_cannot_allow_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy bypass selects direct routing without changing destination authority."""
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.10:3128")
    monkeypatch.setenv("NO_PROXY", "private.example")
    connections = _capture_connections(monkeypatch, {})

    with pytest.raises(ValueError, match="Unable to read worker media resource"):
        _inline_file("http://private.example/media.txt")

    assert connections == []


@pytest.mark.parametrize(
    "location",
    ["http://127.0.0.1/private", "http://169.254.169.254/latest", "http://private.example/private"],
)
def test_worker_media_proxy_rejects_forbidden_redirect(
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    """Redirect destination is rejected before the proxy receives another request."""
    monkeypatch.setenv("ALL_PROXY", "http://10.0.0.10:3128")
    connections = _capture_connections(
        monkeypatch,
        {"10.0.0.10": [_response("302 Found", body=b"", location=location)]},
    )

    with pytest.raises(ValueError, match="Unable to read worker media resource"):
        _inline_file("http://media.example/media.txt")

    assert len(connections) == 1
    assert location.encode() not in connections[0].stream.written
    assert connections[0].stream.closed


@pytest.mark.parametrize("bypass_redirect", [False, True])
def test_worker_media_redirect_reselects_scheme_and_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
    media_proxy_env: list[str],
    bypass_redirect: bool,
) -> None:
    """Redirects keep HTTPS proxy selection and destination-specific bypass rules."""
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.10:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.11:3129")
    if bypass_redirect:
        monkeypatch.setenv("NO_PROXY", "cdn.example")
    next_host = "93.184.216.35" if bypass_redirect else "10.0.0.11"
    next_port = 443 if bypass_redirect else 3129
    next_responses = [_response()] if bypass_redirect else [_CONNECT_OK, _response()]
    connections = _capture_connections(
        monkeypatch,
        {
            "10.0.0.10": [_response("302 Found", body=b"", location="https://cdn.example/final.txt")],
            next_host: next_responses,
        },
    )

    assert _inline_file("http://media.example/media.txt").content == b"media"

    assert [(item.host, item.port) for item in connections] == [("10.0.0.10", 3128), (next_host, next_port)]
    assert connections[1].stream.tls_hosts == ["cdn.example"]
    assert b"host: cdn.example\r\n" in connections[1].stream.written.lower()
    assert media_proxy_env == ["media.example", "cdn.example"]


def test_worker_media_proxy_preserves_byte_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proxy transport cannot expand the existing worker media byte budget."""
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.10:3128")
    monkeypatch.setattr(media_transport, "MAX_MEDIA_BYTES", 4)
    connections = _capture_connections(monkeypatch, {"10.0.0.10": [_response(body=b"12345")]})

    with pytest.raises(ValueError, match="byte limit"):
        _inline_file("http://media.example/media.txt")

    assert len(connections) == 1
    assert connections[0].stream.closed


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_worker_media_proxy_denial_never_retries_direct(
    monkeypatch: pytest.MonkeyPatch,
    scheme: str,
) -> None:
    """Proxy or CONNECT denial remains a tool failure without bypassing policy."""
    monkeypatch.setenv("ALL_PROXY", "http://10.0.0.10:3128")
    connections = _capture_connections(monkeypatch, {"10.0.0.10": [_response("403 Forbidden")]})

    with pytest.raises(ValueError, match="Unable to read worker media resource"):
        _inline_file(f"{scheme}://media.example/media.txt")

    assert [(item.host, item.port) for item in connections] == [("10.0.0.10", 3128)]
    assert connections[0].stream.closed


class _UnreadBodyStream(_RecordingStream):
    """Fail before any body read from a response that should be closed unread."""

    def __init__(self, headers: bytes) -> None:
        super().__init__([headers])
        self.read_calls = 0

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        """Expose header-only progress and reject any attempt to fetch the body."""
        self.read_calls += 1
        assert self.read_calls == 1, "Response body must remain unread"
        return super().read(max_bytes, timeout=timeout)


def _unread_redirect(location: str, framing: str = "Content-Length: 1000000000000") -> _UnreadBodyStream:
    return _UnreadBodyStream(
        (
            "HTTP/1.1 302 Found\r\n"
            f"Location: {location}\r\n"
            f"{framing}\r\n"
            "Content-Encoding: gzip\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii"),
    )


def _capture_scripted_connections(
    monkeypatch: pytest.MonkeyPatch,
    script: list[_Connection],
) -> list[_Connection]:
    connections: list[_Connection] = []

    def connect(
        _backend: httpcore.SyncBackend,
        host: str,
        port: int,
        **_kwargs: object,
    ) -> httpcore.NetworkStream:
        assert len(connections) < len(script), "Unexpected request after the redirect limit"
        expected = script[len(connections)]
        assert (host, port) == (expected.host, expected.port)
        assert all(item.stream.closed for item in connections), "Previous response remains open"
        connections.append(expected)
        return expected.stream

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", connect)
    return connections


@pytest.mark.parametrize("proxy", [False, True])
@pytest.mark.parametrize("framing", ["Content-Length: 1000000000000", "Transfer-Encoding: chunked"])
def test_worker_media_redirect_body_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
    proxy: bool,
    framing: str,
) -> None:
    """Oversized or endless redirects close unread through real HTTPX transports."""
    if proxy:
        monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.10:3128")
    host, port = ("10.0.0.10", 3128) if proxy else ("93.184.216.34", 80)
    monkeypatch.setattr(media_transport, "MAX_MEDIA_BYTES", 4)
    redirect = _unread_redirect("/final.txt", framing)
    final = _RecordingStream([_response(body=b"done")])
    script = [_Connection(host, port, redirect), _Connection(host, port, final)]
    connections = _capture_scripted_connections(monkeypatch, script)

    assert _inline_file("http://media.example/start.txt").content == b"done"

    assert connections == script
    assert redirect.read_calls == 1
    assert all(item.stream.closed for item in connections)
    assert all(b"accept-encoding: identity\r\n" in item.stream.written.lower() for item in connections)
    assert b"/final.txt HTTP/1.1\r\n" in final.written


@pytest.mark.parametrize(("redirect_count", "loop"), [(5, False), (6, False), (6, True)])
def test_worker_media_manual_redirect_limit(
    monkeypatch: pytest.MonkeyPatch,
    redirect_count: int,
    loop: bool,
) -> None:
    """Five hops succeed; a sixth redirect or a loop fails without a seventh request."""
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.10:3128")
    redirects = [_unread_redirect("/start.txt" if loop else f"/hop-{index}.txt") for index in range(redirect_count)]
    streams: list[_RecordingStream] = list(redirects)
    if redirect_count == 5:
        streams.append(_RecordingStream([_response()]))
    script = [_Connection("10.0.0.10", 3128, stream) for stream in streams]
    connections = _capture_scripted_connections(monkeypatch, script)

    if redirect_count == 5:
        assert _inline_file("http://media.example/start.txt").content == b"media"
    else:
        with pytest.raises(ValueError, match="Unable to read worker media resource"):
            _inline_file("http://media.example/start.txt")

    assert connections == script
    assert len(connections) == 6
    assert all(stream.read_calls == 1 for stream in redirects)
    assert all(stream.closed for stream in streams)


def test_worker_media_redirect_final_body_remains_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skipping an oversized redirect does not relax the final media byte limit."""
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.10:3128")
    monkeypatch.setattr(media_transport, "MAX_MEDIA_BYTES", 4)
    redirect = _unread_redirect("/final.txt")
    final = _RecordingStream([_response(body=b"12345")])
    script = [_Connection("10.0.0.10", 3128, redirect), _Connection("10.0.0.10", 3128, final)]
    connections = _capture_scripted_connections(monkeypatch, script)

    with pytest.raises(ValueError, match="byte limit"):
        _inline_file("http://media.example/start.txt")

    assert connections == script
    assert redirect.read_calls == 1
    assert redirect.closed
    assert final.closed


@pytest.mark.parametrize("proxy", [False, True])
def test_worker_media_unread_redirect_still_rejects_private_destination(
    monkeypatch: pytest.MonkeyPatch,
    proxy: bool,
) -> None:
    """Closing a redirect without reading it leaves the next destination guarded."""
    if proxy:
        monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.10:3128")
    host, port = ("10.0.0.10", 3128) if proxy else ("93.184.216.34", 80)
    redirect = _unread_redirect("http://private.example/private.txt")
    script = [_Connection(host, port, redirect)]
    connections = _capture_scripted_connections(monkeypatch, script)

    with pytest.raises(ValueError, match="Unable to read worker media resource"):
        _inline_file("http://media.example/start.txt")

    assert connections == script
    assert redirect.read_calls == 1
    assert redirect.closed


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd", "identity, gzip", "unknown"])
def test_worker_media_encoded_final_body_is_never_read(
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
) -> None:
    """Unsupported HTTP encodings fail before body collection or decompression."""
    monkeypatch.setenv("HTTP_PROXY", "http://10.0.0.10:3128")
    final = _UnreadBodyStream(
        (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Encoding: {encoding}\r\n"
            "Content-Length: 1000000000000\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii"),
    )
    script = [_Connection("10.0.0.10", 3128, final)]
    connections = _capture_scripted_connections(monkeypatch, script)

    with pytest.raises(ValueError, match="identity content encoding"):
        _inline_file("http://media.example/media.txt")

    assert connections == script
    assert final.read_calls == 1
    assert final.closed
    assert b"accept-encoding: identity\r\n" in final.written.lower()


@pytest.mark.parametrize("encoding", ["identity", "Identity", "identity, identity"])
def test_worker_media_identity_encoding_keeps_content(
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
) -> None:
    """Explicit identity coding retains original bytes and MIME metadata."""
    response = _response(body=b"media").replace(
        b"\r\n\r\n",
        f"\r\nContent-Encoding: {encoding}\r\n\r\n".encode("ascii"),
        1,
    )
    connections = _capture_connections(monkeypatch, {"93.184.216.34": [response]})

    result = _inline_file("http://media.example/media.txt")

    assert result.content == b"media"
    assert result.mime_type == "text/plain"
    assert connections[0].stream.closed
