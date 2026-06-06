"""Tests for server-side fetch URL validation."""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock

import pytest

from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_url
from mindroom.tools.crawl4ai import crawl4ai_tools
from mindroom.tools.custom_api import custom_api_tools


def _addrinfo(ip_address: str) -> list[tuple[int, int, int, str, tuple[str, int]]]:
    family = socket.AF_INET6 if ":" in ip_address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip_address, 443))]


def test_validate_server_fetch_url_allows_public_http_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public HTTP(S) URLs should remain valid fetch targets."""
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo("93.184.216.34"),
    )

    assert validate_server_fetch_url("https://example.com/path?token=value") == "https://example.com/path?token=value"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://10.0.0.10/",
        "http://169.254.169.254/latest/meta-data/",
        "http://224.0.0.1/",
        "http://0.0.0.0/",
    ],
)
def test_validate_server_fetch_url_rejects_direct_internal_addresses(url: str) -> None:
    """Direct private, loopback, link-local, multicast, reserved, and metadata IP targets are blocked."""
    with pytest.raises(ServerFetchUrlError):
        validate_server_fetch_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/file",
        "http:///missing-host",
    ],
)
def test_validate_server_fetch_url_rejects_unsupported_or_invalid_urls(url: str) -> None:
    """Only absolute HTTP(S) URLs with hosts should be accepted."""
    with pytest.raises(ServerFetchUrlError):
        validate_server_fetch_url(url)


@pytest.mark.parametrize("url", ["https://example.com:notaport/", "http://example.com:99999/"])
def test_validate_server_fetch_url_rejects_invalid_ports_with_generic_error(url: str) -> None:
    """Malformed ports should use the server-fetch validation error type."""
    with pytest.raises(ServerFetchUrlError) as exc_info:
        validate_server_fetch_url(url)

    assert exc_info.value.reason == "invalid_port"


@pytest.mark.parametrize("url", ["http://[not-ip]/", "http://[::1/"])
def test_validate_server_fetch_url_rejects_malformed_hosts_with_generic_error(url: str) -> None:
    """Malformed bracketed hosts should use the server-fetch validation error type."""
    with pytest.raises(ServerFetchUrlError) as exc_info:
        validate_server_fetch_url(url)

    assert exc_info.value.reason == "invalid_host"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8123/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://metadata/latest/meta-data/",
    ],
)
def test_validate_server_fetch_url_rejects_local_and_metadata_hostnames(url: str) -> None:
    """Local and cloud metadata hostnames should be denied before DNS resolution."""
    with pytest.raises(ServerFetchUrlError):
        validate_server_fetch_url(url)


def test_validate_server_fetch_url_rejects_private_dns_results(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hostnames resolving to private network addresses should be blocked."""
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo("10.0.0.8"),
    )

    with pytest.raises(ServerFetchUrlError):
        validate_server_fetch_url("https://private.example.com/")


def test_validate_server_fetch_url_allows_private_when_explicitly_enabled() -> None:
    """Integrations with a deliberate local-network opt-in can allow private Home Assistant hosts."""
    assert (
        validate_server_fetch_url("http://192.168.1.10:8123/", allow_private_networks=True)
        == "http://192.168.1.10:8123/"
    )


def test_validate_server_fetch_url_keeps_metadata_blocked_when_private_is_enabled() -> None:
    """The local-network opt-in should not open cloud metadata endpoints."""
    with pytest.raises(ServerFetchUrlError):
        validate_server_fetch_url("http://169.254.169.254/latest/meta-data/", allow_private_networks=True)


def test_validate_server_fetch_url_keeps_ipv4_mapped_metadata_blocked_when_private_is_enabled() -> None:
    """The local-network opt-in should not open IPv4-mapped cloud metadata endpoints."""
    with pytest.raises(ServerFetchUrlError) as exc_info:
        validate_server_fetch_url("http://[::ffff:100.100.100.200]/", allow_private_networks=True)

    assert exc_info.value.reason == "metadata_address"


@pytest.mark.parametrize("url", ["http://169.254.1.1/", "http://[fe80::1]/"])
def test_validate_server_fetch_url_keeps_link_local_blocked_when_private_is_enabled(url: str) -> None:
    """The local-network opt-in should not open link-local addresses."""
    with pytest.raises(ServerFetchUrlError) as exc_info:
        validate_server_fetch_url(url, allow_private_networks=True)

    assert exc_info.value.reason == "blocked_address"


def test_validate_server_fetch_url_blocks_metadata_dns_when_private_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local-network opt-in should still reject hostnames resolving to metadata endpoints."""
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo("169.254.169.254"),
    )

    with pytest.raises(ServerFetchUrlError) as exc_info:
        validate_server_fetch_url("https://metadata-by-dns.example/", allow_private_networks=True)

    assert exc_info.value.reason == "metadata_address"


def test_validate_server_fetch_url_blocks_link_local_dns_when_private_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The local-network opt-in should still reject hostnames resolving to link-local addresses."""
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo("169.254.1.1"),
    )

    with pytest.raises(ServerFetchUrlError) as exc_info:
        validate_server_fetch_url("https://link-local-by-dns.example/", allow_private_networks=True)

    assert exc_info.value.reason == "blocked_address"


def test_custom_api_tool_rejects_private_endpoint_before_request() -> None:
    """Custom API requests should use server-fetch URL validation before making requests."""
    tool = custom_api_tools()()

    with pytest.raises(ServerFetchUrlError):
        tool.make_request("http://127.0.0.1:8000/admin")


def test_custom_api_tool_validates_combined_base_url() -> None:
    """Custom API base_url plus endpoint should not bypass server-fetch validation."""
    tool = custom_api_tools()(base_url="http://169.254.169.254")

    with pytest.raises(ServerFetchUrlError):
        tool.make_request("/latest/meta-data")


def test_custom_api_tool_rejects_private_redirect_before_following(monkeypatch: pytest.MonkeyPatch) -> None:
    """Custom API requests should validate each redirect before following it."""
    monkeypatch.setattr(
        "mindroom.server_fetch_url.socket.getaddrinfo",
        lambda *_args, **_kwargs: _addrinfo("93.184.216.34"),
    )
    requested_urls: list[str] = []

    def fake_request(**kwargs: object) -> object:
        requested_urls.append(str(kwargs["url"]))
        return type(
            "RedirectResponse",
            (),
            {"headers": {"Location": "http://127.0.0.1/admin"}, "is_redirect": True},
        )()

    monkeypatch.setattr("requests.request", fake_request)
    tool = custom_api_tools()()

    with pytest.raises(ServerFetchUrlError):
        tool.make_request("https://example.com/redirect")

    assert requested_urls == ["https://example.com/redirect"]


def test_crawl4ai_tool_rejects_private_url_before_crawling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Crawl4AI wrapper should validate URLs before browser crawling starts."""
    tool = crawl4ai_tools()()
    async_crawl = AsyncMock(return_value="ok")
    monkeypatch.setattr(tool, "_async_crawl", async_crawl)

    with pytest.raises(ServerFetchUrlError):
        tool.crawl("http://127.0.0.1:8000/private")

    async_crawl.assert_not_called()
