"""Tests for server-side fetch URL validation."""

from __future__ import annotations

import socket

import pytest

from mindroom.server_fetch_url import ServerFetchUrlError, validate_server_fetch_url


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
