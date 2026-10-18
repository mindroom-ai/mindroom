"""Tests for guarded upstream dialing."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import server_fetch_url
from mindroom.config.egress_broker import EgressBrokerConfig
from mindroom.egress_broker.dial import (
    DestinationBlockedError,
    DialPolicy,
)
from mindroom.server_fetch_url import ServerFetchUrlError
from tests.egress_broker.conftest import (
    connect_request,
    open_upstream,
    plain_request,
    proxy_authorization,
    single_rule_config,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import httpx

    from mindroom.egress_broker.proxy import EgressBroker
    from tests.egress_broker.conftest import BrokerFactory, RawProxy

_PUBLIC = "93.184.216.34"
_OTHER_PUBLIC = "93.184.216.35"

# One entry per direct connection attempt: the host dialed and the keyword arguments of the call.
type _Attempts = list[tuple[str, dict[str, object]]]

# Every destination form the dial guard must refuse, keyed by what it tries.
_REFUSED_DESTINATIONS = {
    "loopback": "127.0.0.1",
    "localhost": "localhost",
    "private": "10.0.0.1",
    "loopback-v6": "::1",
    "ipv4-mapped": "::ffff:127.0.0.1",
    "ipv4-mapped-hex": "::ffff:7f00:1",
    "ipv4-mapped-private": "::ffff:10.0.0.1",
    "ipv4-mapped-metadata": "::ffff:169.254.169.254",
    # These two are refused as reserved ::/8 space, not through an embedded-IPv4 check.
    "ipv4-compatible": "::127.0.0.1",
    "nat64-loopback": "64:ff9b::7f00:1",
    "6to4-loopback": "2002:7f00:1::1",
    # Teredo whose client is 127.0.0.1, stored inverted as 80ff:fffe.
    "teredo-loopback": "2001:0:4136:e378:8000:63bf:80ff:fffe",
    "decimal": "2130706433",
    "octal": "0177.0.0.1",
    "hex": "0x7f000001",
    "hex-dotted": "0x7f.0.0.1",
    "short-form": "127.1",
    "decimal-metadata": "2852039166",
    "octal-metadata": "0251.0376.0251.0376",
    "hex-metadata": "0xa9fea9fe",
    "trailing-dot-localhost": "localhost.",
    "trailing-dot-localhost-upper": "LOCALHOST.",
    "trailing-dots-localhost": "localhost..",
    "trailing-dot-subdomain": "app.localhost.",
    "trailing-dot-ip": "127.0.0.1.",
    # A zone ID is refused for its % alone, before any address check; link-local itself is the next group.
    "zone-id-on-link-local": "fe80::1%eth0",
    "zone-id-on-loopback": "::1%lo",
    "zone-id-url-encoded": "fe80::1%25eth0",
    "metadata": "169.254.169.254",
    "metadata-trailing-dot": "169.254.169.254.",
    "link-local": "169.254.1.1",
    "link-local-v6": "fe80::1",
    "unique-local": "fd00::1",
    "unique-local-top": "fdff:ffff:ffff::1",
    "metadata-v6": "fd00:ec2::254",
    "cgnat-low": "100.64.0.1",
    "cgnat-high": "100.127.255.254",
    "metadata-alibaba": "100.100.100.200",
    "unspecified": "0.0.0.0",  # noqa: S104 - a destination the guard refuses, not a bind address
    "unspecified-short": "0",
    "unspecified-trailing-dot": "0.0.0.0.",
    "unspecified-v6": "::",
    "multicast": "224.0.0.1",
    "multicast-ssdp": "239.255.255.250",
    "multicast-v6-link": "ff02::1",
    "multicast-v6-global": "ff0e::1",
    "multicast-ipv4-mapped": "::ffff:224.0.0.1",
}
# These stay refused when an operator allows private networks and loopback.
_ALWAYS_REFUSED = (
    "ipv4-mapped-metadata",
    "decimal-metadata",
    "zone-id-on-link-local",
    "metadata",
    "link-local",
    "link-local-v6",
    "metadata-v6",
    "metadata-alibaba",
    "unspecified",
    "unspecified-v6",
    "multicast",
    "multicast-v6-global",
    "multicast-ipv4-mapped",
)


@pytest.fixture
def dialed() -> Iterator[_Attempts]:
    """Record every direct connection attempt instead of making it; each succeeds with a fake stream."""
    attempts: _Attempts = []

    async def open_connection(host: str, _port: int, **kwargs: object) -> tuple[AsyncMock, MagicMock]:
        attempts.append((host, kwargs))
        return AsyncMock(spec=asyncio.StreamReader), MagicMock(spec=asyncio.StreamWriter)

    with patch("mindroom.egress_broker.dial.asyncio.open_connection", side_effect=open_connection):
        yield attempts


def _answers(*rounds: list[str]) -> tuple[list[str], Callable[..., list[tuple[object, ...]]]]:
    """Return a lookup log and a fake getaddrinfo that gives the next round of addresses on each call."""
    lookups: list[str] = []
    remaining = list(rounds)

    def getaddrinfo(host: str, port: int, *_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        lookups.append(host)
        addresses = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return lookups, getaddrinfo


@pytest.mark.asyncio
@pytest.mark.parametrize("host", _REFUSED_DESTINATIONS.values(), ids=list(_REFUSED_DESTINATIONS))
async def test_blocked_destinations(host: str, dialed: _Attempts) -> None:
    """Every private, loopback, link-local, metadata, CGNAT, unspecified, and multicast form is refused undialed."""
    with pytest.raises(DestinationBlockedError):
        await open_upstream(host, 443, policy=DialPolicy(), ssl_context=None)

    assert dialed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _ALWAYS_REFUSED)
async def test_metadata_link_local_unspecified_and_multicast_refused_under_the_loosest_policy(
    name: str,
    dialed: _Attempts,
) -> None:
    """Allowing private networks and loopback never opens metadata, link-local, unspecified, or multicast addresses."""
    policy = DialPolicy(allow_private_networks=True, allow_loopback=True)

    with pytest.raises(DestinationBlockedError):
        await open_upstream(_REFUSED_DESTINATIONS[name], 443, policy=policy, ssl_context=None)

    assert dialed == []


# The only refused destinations that listing every address as a private network opens.
_OPENED_BY_PRIVATE_NETWORKS = frozenset(
    {"private", "ipv4-mapped-private", "unique-local", "unique-local-top", "cgnat-low", "cgnat-high"},
)
_EVERY_NETWORK = (ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0"))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _REFUSED_DESTINATIONS)
async def test_private_networks_open_private_addresses_but_never_loopback_or_metadata(
    name: str,
    dialed: _Attempts,
) -> None:
    """Listing every address opens private, unique-local, and CGNAT addresses; loopback in any form stays refused."""
    policy = DialPolicy(private_networks=_EVERY_NETWORK)

    if name in _OPENED_BY_PRIVATE_NETWORKS:
        await open_upstream(_REFUSED_DESTINATIONS[name], 443, policy=policy, ssl_context=None)
        assert len(dialed) == 1
    else:
        with pytest.raises(DestinationBlockedError):
            await open_upstream(_REFUSED_DESTINATIONS[name], 443, policy=policy, ssl_context=None)
        assert dialed == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "opened"),
    [
        (["192.168.1.5"], True),
        (["::ffff:192.168.1.5"], True),
        (["192.168.2.5"], False),
        (["100.64.0.1"], False),
        (["192.168.1.5", "10.0.0.1"], False),
    ],
)
async def test_private_networks_open_only_their_own_addresses(
    answer: list[str],
    *,
    opened: bool,
    dialed: _Attempts,
) -> None:
    """A name resolving to private addresses is dialed only when every one is inside a listed network."""
    policy = DialPolicy(private_networks=(ipaddress.ip_network("192.168.1.0/24"),))
    _lookups, getaddrinfo = _answers(answer)

    with patch.object(server_fetch_url.socket, "getaddrinfo", side_effect=getaddrinfo):
        if opened:
            await open_upstream("nas.lab.example", 443, policy=policy, ssl_context=None)
        else:
            with pytest.raises(DestinationBlockedError):
                await open_upstream("nas.lab.example", 443, policy=policy, ssl_context=None)

    assert [host for host, _kwargs in dialed] == (answer if opened else [])


@pytest.mark.asyncio
async def test_dns_rebinding_dials_exactly_the_checked_address(dialed: _Attempts) -> None:
    """A name that answers public, then loopback, is resolved once and dialed at the public address it was checked as.

    TLS still verifies the name: it is the SNI, while the socket goes to the checked IP.
    """
    lookups, getaddrinfo = _answers([_PUBLIC], ["127.0.0.1"])

    with patch.object(server_fetch_url.socket, "getaddrinfo", side_effect=getaddrinfo):
        await open_upstream("rebind.example", 443, policy=DialPolicy(), ssl_context=ssl.create_default_context())

    assert lookups == ["rebind.example"]
    assert [(host, kwargs["server_hostname"]) for host, kwargs in dialed] == [(_PUBLIC, "rebind.example")]


@pytest.mark.asyncio
async def test_dns_rebinding_after_a_private_answer_stays_refused(dialed: _Attempts) -> None:
    """A name whose checked answer is private is refused, even though asking again would answer public."""
    lookups, getaddrinfo = _answers(["127.0.0.1"], [_PUBLIC])

    with (
        patch.object(server_fetch_url.socket, "getaddrinfo", side_effect=getaddrinfo),
        pytest.raises(DestinationBlockedError),
    ):
        await open_upstream("rebind.example", 443, policy=DialPolicy(), ssl_context=None)

    assert lookups == ["rebind.example"]
    assert dialed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("private", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fd00::1"])
async def test_one_private_address_in_the_answer_refuses_the_name(
    private: str,
    dialed: _Attempts,
) -> None:
    """A mixed answer is refused outright, so the dial cannot fall back from the public address to the private one."""
    _lookups, getaddrinfo = _answers([_PUBLIC, private])

    with (
        patch.object(server_fetch_url.socket, "getaddrinfo", side_effect=getaddrinfo),
        pytest.raises(DestinationBlockedError),
    ):
        await open_upstream("mixed.example", 443, policy=DialPolicy(), ssl_context=None)

    assert dialed == []


@pytest.mark.asyncio
async def test_fallback_dials_the_next_checked_address_without_resolving_again() -> None:
    """When the first checked address fails, the next one from the same answer is dialed without resolving again."""
    lookups, getaddrinfo = _answers([_PUBLIC, _OTHER_PUBLIC], ["127.0.0.1"])
    attempts: list[str] = []

    async def open_connection(host: str, _port: int, **_kwargs: object) -> tuple[AsyncMock, MagicMock]:
        attempts.append(host)
        if len(attempts) == 1:
            msg = "Connection refused"
            raise OSError(msg)
        return AsyncMock(spec=asyncio.StreamReader), MagicMock(spec=asyncio.StreamWriter)

    with (
        patch.object(server_fetch_url.socket, "getaddrinfo", side_effect=getaddrinfo),
        patch("mindroom.egress_broker.dial.asyncio.open_connection", side_effect=open_connection),
    ):
        await open_upstream("rebind.example", 443, policy=DialPolicy(), ssl_context=None)

    assert lookups == ["rebind.example"]
    assert attempts == [_PUBLIC, _OTHER_PUBLIC]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "status"),
    [
        ("[::ffff:127.0.0.1]:443", 403),
        ("[::ffff:7f00:1]:443", 403),
        ("2130706433:443", 403),
        ("0177.0.0.1:443", 403),
        ("0x7f.0.0.1:443", 403),
        ("localhost.:443", 403),
        ("[fe80::1%25eth0]:443", 400),
        ("169.254.169.254:80", 403),
        ("[fd00::1]:443", 403),
        ("100.64.0.1:443", 403),
        ("0.0.0.0:443", 403),
        ("224.0.0.1:443", 403),
    ],
)
@pytest.mark.parametrize("transport", ["connect", "plain"])
async def test_broker_refuses_blocked_destinations_in_every_form(
    broker_default_policy: EgressBroker,
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    target: str,
    status: int,
    transport: str,
) -> None:
    """CONNECT and absolute-form targets reach one guard: blocked forms get 403, zone IDs 400, and nothing is dialed."""
    authorization = proxy_authorization(broker.token())
    request = (
        connect_request(target, authorization=authorization)
        if transport == "connect"
        else plain_request(target, authorization=authorization)
    )

    with patch("mindroom.egress_broker.dial._dial", new_callable=AsyncMock) as dial_checked_address:
        response = await raw_proxy(broker_default_policy.port, request)

    assert response.status == status
    assert response.json()["error"] == ("destination_blocked" if status == 403 else "bad_request")
    dial_checked_address.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["connect", "plain"])
async def test_broker_dials_private_networks_the_config_lists(
    broker: BrokerFactory,
    raw_proxy: RawProxy,
    transport: str,
) -> None:
    """The broker reads `private_networks` from the config on each request, so a listed address is dialed."""
    listed = EgressBrokerConfig(private_networks=["10.0.0.0/8"])
    config = EgressBrokerConfig()
    started = await broker(dial_policy=DialPolicy(), rules_provider=lambda _claims: config)
    authorization = proxy_authorization(broker.token())
    send = connect_request if transport == "connect" else plain_request
    unreachable = OSError("Connection refused")

    with patch("mindroom.egress_broker.dial._dial", new_callable=AsyncMock, side_effect=unreachable) as dial:
        refused = await raw_proxy(started.port, send("10.0.0.1:443", authorization=authorization))
        config = listed
        dialed = await raw_proxy(started.port, send("10.0.0.1:443", authorization=authorization))

    assert refused.json()["error"] == "destination_blocked"
    assert dialed.status == 502
    assert [call.args[1] for call in dial.call_args_list] == [ipaddress.ip_address("10.0.0.1")]


@pytest.mark.asyncio
async def test_broker_injects_keys_for_hosts_in_listed_private_networks(
    broker: BrokerFactory,
    proxy_client: Callable[..., httpx.AsyncClient],
) -> None:
    """An intercepted request to a rule's private host is dialed once the config lists its network."""
    config = single_rule_config("10.0.0.1")
    await broker(dial_policy=DialPolicy(), rules_provider=lambda _claims: config, secrets={"svc": "s"})
    client = proxy_client(broker.token())

    with patch("mindroom.egress_broker.dial._dial", new_callable=AsyncMock, side_effect=OSError("refused")) as dial:
        refused = await client.get("https://10.0.0.1/")
        config = config.model_copy(update={"private_networks": ["10.0.0.0/8"]})
        dialed = await client.get("https://10.0.0.1/")

    assert refused.json()["error"] == "destination_blocked"
    assert dialed.json()["error"] == "upstream_unreachable"
    assert [call.args[1] for call in dial.call_args_list] == [ipaddress.ip_address("10.0.0.1")]


@pytest.mark.asyncio
async def test_loopback_allowed_when_policy_allows(closing_listener: int) -> None:
    """Loopback connects when policy allows it."""
    policy = DialPolicy(allow_loopback=True)
    _reader, writer = await open_upstream("127.0.0.1", closing_listener, policy=policy, ssl_context=None)
    writer.close()
    await writer.wait_closed()


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
async def test_unresolvable_hostname_raises_os_error() -> None:
    """An unresolvable hostname raises a plain OSError, so the broker answers it as an unreachable upstream."""
    unresolvable = ServerFetchUrlError(reason="dns_resolution_failed")

    with patch("mindroom.egress_broker.dial.validated_connect_addresses", side_effect=unresolvable):
        policy = DialPolicy()
        with pytest.raises(OSError, match="URL is not allowed") as exc_info:
            await open_upstream("nonexistent.invalid", 443, policy=policy, ssl_context=None)

        # The resolution failure is the cause, not a dial attempt.
        assert exc_info.value.__cause__ is unresolvable
