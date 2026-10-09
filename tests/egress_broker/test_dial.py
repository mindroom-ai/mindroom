"""Tests for guarded upstream dialing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.egress_broker.dial import (
    DestinationBlockedError,
    DestinationUnresolvableError,
    DialPolicy,
    open_upstream,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"],
)
async def test_blocked_destinations(host: str) -> None:
    """Blocked destinations raise DestinationBlockedError."""
    with pytest.raises(DestinationBlockedError):
        await open_upstream(host, 443, policy=DialPolicy(), ssl_context=None)


@pytest.mark.asyncio
async def test_loopback_allowed_when_policy_allows() -> None:
    """Loopback connects when policy allows it."""
    # Start a local test server
    server = await asyncio.start_server(
        lambda _r, w: w.close(),
        "127.0.0.1",
        0,
    )
    try:
        port = server.sockets[0].getsockname()[1]
        policy = DialPolicy(allow_loopback=True)
        _reader, writer = await open_upstream("127.0.0.1", port, policy=policy, ssl_context=None)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_dials_validated_address_not_name() -> None:
    """open_upstream dials the validated IP, not the hostname."""
    # Mock validated_connect_addresses to return a specific IP
    mock_addresses = [MagicMock(compressed="127.0.0.1", version=4)]

    # Track what asyncio.open_connection receives
    connection_calls = []
    original_open_connection = asyncio.open_connection

    async def mock_open_connection(
        host: str,
        port: int,
        **_kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        connection_calls.append({"host": host, "port": port})
        # Create a real connection to localhost for the test
        return await original_open_connection("127.0.0.1", port, **_kwargs)

    # Start a local test server
    server = await asyncio.start_server(
        lambda _r, w: w.close(),
        "127.0.0.1",
        0,
    )
    try:
        port = server.sockets[0].getsockname()[1]

        with (
            patch("mindroom.egress_broker.dial.validated_connect_addresses", return_value=mock_addresses),
            patch("mindroom.egress_broker.dial.asyncio.open_connection", side_effect=mock_open_connection),
        ):
            policy = DialPolicy(allow_loopback=True)
            _reader, writer = await open_upstream("example.com", port, policy=policy, ssl_context=None)
            writer.close()
            await writer.wait_closed()

        # Verify it connected to the IP, not the hostname
        assert len(connection_calls) == 1
        assert connection_calls[0]["host"] == "127.0.0.1"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_falls_back_to_next_address() -> None:
    """When the first address fails, open_upstream tries the next one."""
    # Two mock addresses: first one fails, second succeeds
    mock_addresses = [
        MagicMock(compressed="127.0.0.1", version=4),
        MagicMock(compressed="127.0.0.2", version=4),
    ]

    connection_calls = []
    call_count = [0]

    async def mock_open_connection(
        host: str,
        _port: int,
        **_kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        connection_calls.append(host)
        call_count[0] += 1
        if call_count[0] == 1:
            # First call fails
            msg = "Connection refused"
            raise OSError(msg)
        # Second call succeeds - create a mock connection
        reader = AsyncMock(spec=asyncio.StreamReader)
        writer = MagicMock(spec=asyncio.StreamWriter)
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        return reader, writer

    with (
        patch("mindroom.egress_broker.dial.validated_connect_addresses", return_value=mock_addresses),
        patch("mindroom.egress_broker.dial.asyncio.open_connection", side_effect=mock_open_connection),
    ):
        policy = DialPolicy(allow_loopback=True)
        _reader, writer = await open_upstream("example.com", 443, policy=policy, ssl_context=None)
        writer.close()
        await writer.wait_closed()

    # Verify both addresses were tried
    assert connection_calls == ["127.0.0.1", "127.0.0.2"]


@pytest.mark.asyncio
async def test_connect_timeout_raises_oserror() -> None:
    """When all addresses fail to connect within the timeout, OSError is raised."""
    mock_addresses = [MagicMock(compressed="127.0.0.1", version=4)]

    async def mock_open_connection(
        _host: str,
        _port: int,
        **_kwargs: object,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # Simulate a connection that takes too long
        await asyncio.sleep(20)
        msg = "Should not reach here"
        raise OSError(msg)

    with (
        patch("mindroom.egress_broker.dial.validated_connect_addresses", return_value=mock_addresses),
        patch("mindroom.egress_broker.dial.asyncio.open_connection", side_effect=mock_open_connection),
    ):
        policy = DialPolicy(connect_timeout=0.1)
        with pytest.raises((OSError, TimeoutError)):
            await open_upstream("example.com", 443, policy=policy, ssl_context=None)


@pytest.mark.asyncio
async def test_unresolvable_hostname_raises_destination_unresolvable_error() -> None:
    """Unresolvable hostname raises DestinationUnresolvableError which is an OSError."""

    # Create a mock ValueError that simulates ServerFetchUrlError with reason="dns_resolution_failed"
    def mock_validated_connect_addresses(*_args: object, **_kwargs: object) -> list[object]:
        exc = ValueError("URL is not allowed for server-side fetching")
        exc.reason = "dns_resolution_failed"  # type: ignore[attr-defined]
        raise exc

    with patch("mindroom.egress_broker.dial.validated_connect_addresses", side_effect=mock_validated_connect_addresses):
        policy = DialPolicy()
        with pytest.raises(DestinationUnresolvableError) as exc_info:
            await open_upstream("nonexistent.invalid", 443, policy=policy, ssl_context=None)

        # Verify it's an OSError subclass
        assert isinstance(exc_info.value, OSError)


@pytest.mark.asyncio
async def test_blocked_address_still_raises_destination_blocked_error() -> None:
    """Blocked address raises DestinationBlockedError, not DestinationUnresolvableError."""

    # Create a mock ValueError that simulates ServerFetchUrlError with a validation failure reason
    def mock_validated_connect_addresses(*_args: object, **_kwargs: object) -> list[object]:
        exc = ValueError("URL is not allowed for server-side fetching")
        exc.reason = "private_address"  # type: ignore[attr-defined]
        raise exc

    with patch("mindroom.egress_broker.dial.validated_connect_addresses", side_effect=mock_validated_connect_addresses):
        policy = DialPolicy()
        with pytest.raises(DestinationBlockedError):
            await open_upstream("10.0.0.1", 443, policy=policy, ssl_context=None)
