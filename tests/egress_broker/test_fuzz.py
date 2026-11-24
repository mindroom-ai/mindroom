"""Property-based fuzzing of the egress broker: path routing against server normalization, and malformed requests."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import itertools
import posixpath
import re
import ssl
import unicodedata
from typing import TYPE_CHECKING
from urllib.parse import quote, unquote

import pytest
import pytest_asyncio
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs

from mindroom import server_fetch_url
from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService
from mindroom.egress_broker import dial
from mindroom.egress_broker.mitm import _host_header_name
from mindroom.egress_broker.proxy import _token_from
from mindroom.egress_broker.rules import is_ambiguous_path, route_request
from mindroom.egress_broker.tokens import TokenSigner
from tests.egress_broker.conftest import (
    DEFAULT_CLAIMS,
    connect_request,
    proxy_authorization,
    single_rule_config,
    with_user_services,
)
from tests.egress_broker.raw_http import open_tunnel

if TYPE_CHECKING:
    from mindroom.egress_broker.dial import DialPolicy
    from mindroom.egress_broker.proxy import EgressBroker
    from tests.egress_broker.conftest import BrokerFactory, SeenRequest, Upstream

# One fixed profile for every property here, so a CI run explores the same examples each time.
FUZZ = settings(
    derandomize=True,
    database=None,
    deadline=None,
    print_blob=True,
    max_examples=1000,
    suppress_health_check=[HealthCheck.too_slow],
)
# Properties that drive a live broker share one listener and upstream across examples, and run fewer of them.
FUZZ_LIVE = settings(
    FUZZ,
    max_examples=100,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)

# Path routing against server-side normalization.

HOST = "api.example"
PREFIXES = ("/private", "/private/", "/a/b", "/repos/o/r", "/v1/")

# Look-alikes NFKC folds to ASCII, spelled as escapes so they cannot be misread: one and two dot leaders,
# fullwidth and small full stops, the ideographic full stop (which NFKC keeps), and fullwidth / \ and %2e.
ONE_DOT_LEADER = "\u2024"
TWO_DOT_LEADER = "\u2025"
FULLWIDTH_STOP = "\uff0e"
SMALL_STOP = "\ufe52"
IDEOGRAPHIC_STOP = "\u3002"
FULLWIDTH_SLASH = "\uff0f"
FULLWIDTH_BACKSLASH = "\uff3c"
FULLWIDTH_PERCENT_2E = "\uff05\uff12\uff45"
# Combining marks compose with a preceding hex letter under NFKC (C + U+0301 is one character), which can break an
# escape such as %5C apart in the folded form while the unfolded form still decodes it.
COMBINING_MARKS = ("\u0301", "\u0307", "\u0327")
_UNICODE_DOTS = (ONE_DOT_LEADER, TWO_DOT_LEADER, FULLWIDTH_STOP, SMALL_STOP, IDEOGRAPHIC_STOP)
# The character alphabet: path syntax, hex digits for %2e %2f %5c %25, and the unicode dots.
_ALPHABET = st.sampled_from([*"/.%2eEfF5cC;?#ab-", *_UNICODE_DOTS])
# Whole tokens reach the forms a character alphabet rarely spells: encoded ; ? #, overlong and UTF-8 dots, NUL.
_TOKENS = st.sampled_from(
    [
        ".",
        "..",
        "%2e",
        "%2E",
        "%252e",
        "%25252E",
        "/",
        "%2f",
        "%2F",
        "%252f",
        "\\",
        "%5c",
        "%5C",
        "%255c",
        ";",
        "%3b",
        "%253B",
        "?",
        "%3f",
        "%3F",
        "#",
        "%23",
        " ",
        "%20",
        "%09",
        "%00",
        "%c0%ae",
        "%e0%80%ae",
        "%EF%BC%8E",
        "%E2%80%A4",
        "%E2%80%A5",
        "%EF%BC%8F",
        "%EF%BC%BC",
        "%EF%BC%85",
        *_UNICODE_DOTS,
        FULLWIDTH_STOP * 2,
        ONE_DOT_LEADER * 2,
        FULLWIDTH_STOP + "%2e",
        "%EF%BC%8E%EF%BC%8E",
        FULLWIDTH_PERCENT_2E * 2,
        FULLWIDTH_SLASH,
        FULLWIDTH_BACKSLASH,
        FULLWIDTH_PERCENT_2E,
        *COMBINING_MARKS,
        "%CC%81",
        "%CC%87",
        "%CC%A7",
        "a",
        "echo",
        "private",
    ],
)


def _cut_whole(path: str) -> str:
    """Drop everything from the first ``;``, ``?``, or ``#``, as servers that treat ``;`` as a query start do."""
    return re.split(r"[;?#]", path, maxsplit=1)[0]


def _cut_segments(path: str) -> str:
    """Drop the query and fragment, then each segment's ``;`` parameters, as Tomcat-style servers do."""
    head = re.split(r"[?#]", path, maxsplit=1)[0]
    return "/".join(segment.split(";", 1)[0] for segment in head.replace("\\", "/").split("/"))


# Each step is one thing a server may do to a path; a server may do any of them, in any order, any number of times.
_SERVER_STEPS = (
    unquote,
    _cut_whole,
    _cut_segments,
    lambda path: path.replace("\\", "/"),
    lambda path: unicodedata.normalize("NFKC", path),
)


def server_views(path: str) -> set[str]:
    """Return every path a server could resolve `path` to under the modeled normalization steps.

    The steps are repeated percent-decoding, cutting at ``;?#`` (whole path or per segment), backslash as slash,
    and NFKC folding (unicode dots and slashes); every composition is explored, then `posixpath.normpath` applies.
    """
    seen = {path}
    frontier = {path}
    while frontier:
        frontier = {step(candidate) for candidate in frontier for step in _SERVER_STEPS} - seen
        seen |= frontier
    return {posixpath.normpath(candidate.replace("\\", "/")) for candidate in seen}


def _segments(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def _stays_under(prefix: str, view: str) -> bool:
    """Return whether a resolved path is the prefix or below it, compared by whole segments."""
    wanted = _segments(posixpath.normpath(prefix))
    return _segments(view)[: len(wanted)] == wanted


def _service(prefixes: list[str], *, restrict: bool) -> EgressService:
    rules = [EgressRule(host=HOST, path_prefix=prefix, auth=EgressAuth(type="bearer")) for prefix in prefixes]
    return EgressService(restrict_to_rules=restrict, rules=rules)


_TOKEN_SEGMENTS = st.lists(st.lists(_TOKENS, min_size=1, max_size=3).map("".join), max_size=4).map("/".join)
_PATHS = st.one_of(
    st.builds(lambda prefix, rest: prefix + rest, st.sampled_from(PREFIXES), st.text(_ALPHABET, max_size=24)),
    st.builds(lambda rest: "/" + rest, st.text(_ALPHABET, max_size=24)),
    st.builds(lambda prefix, rest: prefix.rstrip("/") + "/" + rest, st.sampled_from(PREFIXES), _TOKEN_SEGMENTS),
)


@FUZZ
@given(
    path=_PATHS,
    operator_prefixes=st.lists(st.sampled_from(PREFIXES), min_size=1, max_size=2, unique=True),
    operator_restricts=st.booleans(),
    user_prefixes=st.lists(st.sampled_from(PREFIXES), max_size=2, unique=True),
    user_restricts=st.booleans(),
)
# Shrunk counterexample before the NFKC fix: the two dot leader folds to "..".
@example(
    path="/private/" + TWO_DOT_LEADER,
    operator_prefixes=["/private"],
    operator_restricts=False,
    user_prefixes=[],
    user_restricts=False,
)
@example(
    path="/private/%EF%BC%8E%EF%BC%8E/echo",
    operator_prefixes=["/private"],
    operator_restricts=True,
    user_prefixes=[],
    user_restricts=False,
)
# Review counterexample to the first NFKC fix: folding C + U+0301 hid the %5C that decodes to a backslash.
@example(
    path="/private/..%255C%CC%81/echo",
    operator_prefixes=["/private"],
    operator_restricts=False,
    user_prefixes=[],
    user_restricts=False,
)
def test_routed_paths_stay_under_their_prefix_after_server_normalization(
    path: str,
    operator_prefixes: list[str],
    operator_restricts: bool,
    user_prefixes: list[str],
    user_restricts: bool,
) -> None:
    """A path routed under a prefix resolves under it on every modeled server, or `route_request` refuses it.

    This holds for the matched rule's prefix (the one whose secret is injected) and for each party's restriction:
    a path that passes an operator or user `restrict_to_rules` service resolves under one of that party's prefixes.
    """
    # Both callers hand `route_request` the origin-form target without its query.
    path = path.split("?", 1)[0]
    operator = EgressBrokerConfig(services={"op": _service(operator_prefixes, restrict=operator_restricts)})
    user = {"mine": _service(user_prefixes, restrict=user_restricts)} if user_prefixes else {}
    rules = with_user_services(operator, **user)

    route = route_request(rules, HOST, 443, path)

    assert route.host_has_rules
    if route.refusal is not None:
        return
    required = []
    if route.match is not None:
        required.append([route.match.rule.path_prefix])
    if operator_restricts:
        required.append(operator_prefixes)
    if user_prefixes and user_restricts:
        required.append(user_prefixes)
    for view in server_views(path):
        for prefixes in required:
            assert any(_stays_under(prefix, view) for prefix in prefixes), (path, view, prefixes)


@pytest.mark.parametrize(
    ("path", "ambiguous"),
    [
        ("/private/" + TWO_DOT_LEADER, True),
        (f"/private/{FULLWIDTH_STOP * 2}/echo", True),
        (f"/private/{ONE_DOT_LEADER * 2}/echo", True),
        ("/private/%E2%80%A5/echo", True),
        (f"/private/x{FULLWIDTH_SLASH}{TWO_DOT_LEADER}{FULLWIDTH_SLASH}y", True),
        (f"/private/{FULLWIDTH_PERCENT_2E * 2}/echo", True),
        ("/private/..%255C%CC%81/echo", True),
        ("/private/..%252f%CC%87", True),
        ("/private/..%255C\u0327/echo", True),
        ("/wiki/Caf\u00e9", False),
        ("/wiki/Caf%C3%A9", False),
        ("/files/\ufb01le", False),
        ("/private/" + IDEOGRAPHIC_STOP, False),
    ],
)
def test_nfkc_dot_segments_are_ambiguous(path: str, *, ambiguous: bool) -> None:
    """Dots and slashes that NFKC folds to ASCII count as dot segments; other non-ASCII paths stay allowed."""
    assert is_ambiguous_path(path) is ambiguous


def _had_dot_segment_at_d64dbefb7(path: str) -> bool:
    return any(
        re.split(r"[;?#]", segment, maxsplit=1)[0].strip() in {".", ".."} for segment in re.split(r"[/\\]", path)
    )


def _refused_at_d64dbefb7(path: str) -> bool:
    """Return `is_ambiguous_path` as of d64dbefb7, before NFKC folding, kept verbatim as a regression oracle."""
    if "\\" in path or "//" in path:
        return True
    layer = path
    for _ in range(5):
        if "\x00" in layer or _had_dot_segment_at_d64dbefb7(layer):
            return True
        try:
            decoded = unquote(layer, errors="strict")
        except UnicodeDecodeError:
            return True
        if decoded == layer:
            return False
        layer = decoded
    return True


@FUZZ
@given(path=_PATHS)
@example(path="/private/..%255C%CC%81/echo")
@example(path="/private/..%252f%CC%87")
def test_every_path_refused_before_nfkc_folding_is_still_refused(path: str) -> None:
    """Folding look-alikes only adds refusals: whatever the d64dbefb7 check refused, the current one refuses too."""
    if _refused_at_d64dbefb7(path):
        assert is_ambiguous_path(path), path


# Short pieces whose combinations reach escapes split by combining marks, such as ``..%5C`` + U+0301.
_ESCAPE_ATOMS = ("/", ".", "..", "%", "%25", "%2", "%5", "5", "c", "C", "e", "f", "\u0301", "\u0307", "%CC%81", "a")


def test_every_short_escape_combination_refused_before_nfkc_folding_is_still_refused() -> None:
    """Exhaustively, every path of up to four atoms after a prefix keeps the refusal the d64dbefb7 check gave it."""
    lost = [
        path
        for length in range(1, 5)
        for atoms in itertools.product(_ESCAPE_ATOMS, repeat=length)
        if _refused_at_d64dbefb7(path := "/private/" + "".join(atoms)) and not is_ambiguous_path(path)
    ]

    assert lost == []


# Token and Host header parsing.

_SIGNER = TokenSigner(b"k" * 32)
_TOKEN = _SIGNER.mint(DEFAULT_CLAIMS, now=1_000_000.0)


@FUZZ
@given(
    value=st.one_of(
        st.binary(max_size=64),
        st.builds(
            lambda scheme, edit: scheme + edit,
            st.sampled_from([b"Basic ", b"Bearer ", b"basic  ", b"BEARER\t", b""]),
            st.binary(max_size=48),
        ),
        st.builds(
            lambda position, replacement: (_TOKEN[: position % len(_TOKEN)] + replacement).encode("latin-1"),
            st.integers(0, len(_TOKEN)),
            st.text(st.characters(max_codepoint=255), max_size=4),
        ).map(lambda token: b"Bearer " + token),
    ),
)
def test_proxy_authorization_never_raises_and_only_the_minted_token_verifies(value: bytes) -> None:
    """Any Proxy-Authorization value parses to a token or None, and only the exact minted token verifies."""
    token = _token_from([(b"proxy-authorization", value)])

    if token is not None:
        claims = _SIGNER.verify(token, now=1_000_000.0)
        assert claims is None or token == _TOKEN


@FUZZ
@given(
    value=st.one_of(
        st.binary(max_size=32),
        st.builds(
            lambda before, after: before + b"localhost" + after,
            st.sampled_from([b"", b"[", b"user@", b" ", b"LOCAL"]),
            st.one_of(
                st.binary(max_size=12),
                st.sampled_from([b".", b":443", b".:8443", b"@evil", b"\x00", b"..", b"]", b":\xb2", b":4\xb943"]),
            ),
        ),
    ),
)
def test_host_header_names_localhost_only_when_it_says_nothing_else(value: bytes) -> None:
    """A Host header maps to the tunnel host ``localhost`` only as ``localhost`` with one trailing dot and a port."""
    if _host_header_name(value) == "localhost":
        assert re.fullmatch(rb"localhost\.?(:\d{0,5})?", value, re.IGNORECASE), value


@pytest.mark.parametrize("value", [b"localhost:\xb2", b"localhost:4\xb943", b"[::1]:\xb3"])
def test_host_header_port_of_non_ascii_digits_is_malformed(value: bytes) -> None:
    """A Host port is ASCII digits only (H2-1): a superscript digit in the port is malformed, not the tunnel host."""
    assert _host_header_name(value) is None


# Mutated raw requests against a live broker.

SECRET = "s3cret-fuzz"  # noqa: S105 - test credential injected by the broker
_RULE_PREFIX = "/ok"
_LINE_BOUNDARY = re.compile(rb"\n")
_INSERTIONS = st.sampled_from(
    [
        b"\r\n",
        b"\n",
        b"\r",
        b"\r\n\r\n",
        b" ",
        b"\t",
        b":",
        b",",
        b"\x00",
        b"\x7f",
        b"\xff",
        b"Transfer-Encoding: chunked\r\n",
        b"Transfer-Encoding: chunked, identity\r\n",
        b"Transfer-Encoding : chunked\r\n",
        b" chunked",
        b"Content-Length: 4\r\n",
        b"Content-Length: +4\r\n",
        b"Content-Length: 4, 5\r\n",
        b"Content-Length: 99999999999999999999\r\n",
        b"4\r\nwxyz\r\n0\r\n\r\n",
        b"ffffffffffffffffff\r\n",
        b"\r\n x-folded: y",
        b"Expect: 100-continue\r\n",
        b"Upgrade: h2c\r\nConnection: Upgrade\r\n",
        b"Upgrade: websocket\r\nConnection: Upgrade\r\n",
        b"Host: evil.example\r\n",
        b"Proxy-Authorization: Basic Og==\r\n",
        b"Authorization: Bearer client-supplied\r\n",
        b"GET /ok HTTP/1.1\r\nHost: evil.example\r\n\r\n",
        b"HTTP/1.0",
        b"/../",
        b"/%2e%2e/",
        b"%2f",
        b"?x=1",
        b"#",
        b"http://evil.example",
        b"evil.example:443",
        b"[::1]",
        b"%25",
    ],
)
# Each edit replaces up to 8 bytes at an offset into one line of the template with random or adversarial bytes.
_EDITS = st.lists(
    st.tuples(
        st.integers(0, 16),
        st.integers(0, 48),
        st.integers(0, 8),
        st.one_of(st.binary(max_size=12), _INSERTIONS),
    ),
    min_size=1,
    max_size=3,
)


def _mutate(template: bytes, edits: list[tuple[int, int, int, bytes]]) -> bytes:
    data = bytearray(template)
    for line, offset, removed, inserted in edits:
        starts = [0, *(match.end() for match in _LINE_BOUNDARY.finditer(data))]
        start = min(starts[line % len(starts)] + offset, len(data))
        data[start : start + removed] = inserted
    return bytes(data)


def _resolve_only_localhost(host: str, *, port: int | None, scheme: str) -> list[ipaddress.IPv4Address]:
    """Resolve ``localhost`` to 127.0.0.1 and nothing else, so mutated host names never reach real DNS."""
    del port, scheme
    if host == "localhost":
        return [ipaddress.IPv4Address("127.0.0.1")]
    raise server_fetch_url.ServerFetchUrlError(reason="dns_resolution_failed")


@pytest_asyncio.fixture
async def fuzz_broker(
    broker: BrokerFactory,
    monkeypatch: pytest.MonkeyPatch,
    http_upstream: Upstream,
    tls_upstream: Upstream,
) -> EgressBroker:
    """Return one broker for every example of a property: ``localhost/ok`` gets SECRET.

    It stays hermetic: DNS resolves only ``localhost``, and of the loopback ports the broker may dial, only the two
    fake upstreams' answer; a mutated port gets connection refused instead of reaching a local service.
    """
    monkeypatch.setattr(server_fetch_url, "_resolve_host_addresses", _resolve_only_localhost)
    upstream_ports = {http_upstream.port, tls_upstream.port}
    dial_checked_address = dial._dial

    async def dial_fake_upstreams_only(
        host: str,
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
        port: int,
        *,
        policy: DialPolicy,
        ssl_context: ssl.SSLContext | None,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if port not in upstream_ports:
            msg = f"Port {port} is not a fake upstream."
            raise ConnectionRefusedError(msg)
        return await dial_checked_address(host, address, port, policy=policy, ssl_context=ssl_context)

    monkeypatch.setattr(dial, "_dial", dial_fake_upstreams_only)
    return await broker(single_rule_config(path_prefix=_RULE_PREFIX), secrets={"svc": SECRET})


async def _read_available(reader: asyncio.StreamReader, *, seconds: float) -> bytes:
    """Return what the broker sends until it closes, the connection fails, or `seconds` pass."""
    chunks: list[bytes] = []
    with contextlib.suppress(TimeoutError, OSError, ssl.SSLError):
        async with asyncio.timeout(seconds):
            while chunk := await reader.read(64 * 1024):
                chunks.append(chunk)
    return b"".join(chunks)


async def _send_plain(port: int, data: bytes) -> bytes:
    """Send `data` on a fresh connection, half-close it, and return the broker's answer."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        writer.write_eof()
        return await _read_available(reader, seconds=2.0)
    finally:
        writer.transport.abort()


async def _send_in_tunnel(broker: BrokerFactory, upstream: Upstream, data: bytes, *, seconds: float = 0.3) -> bytes:
    """Open an intercepted tunnel to ``localhost``, send `data` inside it, and return the broker's answer.

    TLS cannot be half-closed, so a request the broker keeps waiting on is cut off after `seconds`.
    """
    client = await open_tunnel(broker, upstream.port)
    try:
        client.send(data)
        return await _read_available(client.reader, seconds=seconds)
    finally:
        client.close()


async def _wait_until_idle(broker: EgressBroker) -> None:
    """Wait until the broker has finished every connection, so a late forward is checked with the input behind it."""
    async with asyncio.timeout(5):
        while broker._connections:  # noqa: ASYNC110 - the broker exposes no event for this
            await asyncio.sleep(0.005)


def _assert_no_broker_fault(logs: list[dict[str, object]], received: bytes) -> None:
    """Malformed input is the client's error: the broker never reports an internal fault for it."""
    assert not [log for log in logs if log["event"] == "egress_broker_connection_error"], logs
    assert b"broker_error" not in received


def _assert_upstream_never_saw_credentials(requests: list[SeenRequest], token: str) -> None:
    """The worker's proxy token never leaves the broker, and SECRET only travels as one Bearer to ``localhost/ok``."""
    for seen in requests:
        values = [value for values in seen.headers.values() for value in values]
        assert "proxy-authorization" not in seen.headers
        assert not [value for value in values if token in value]
        if any(SECRET in value for value in values):
            assert seen.headers.get("authorization") == [f"Bearer {SECRET}"], seen
            assert [value for value in values if SECRET in value] == [f"Bearer {SECRET}"], seen
            assert re.fullmatch(r"localhost\.?(:\d+)?", seen.headers["host"][0], re.IGNORECASE), seen
            # The raw target (without its query, as the broker routed it) and aiohttp's decoded path both count.
            views = server_views(seen.raw_path.split("?", 1)[0]) | server_views(seen.path)
            assert all(_stays_under(_RULE_PREFIX, view) for view in views), seen


async def _assert_plain_request_served(broker: BrokerFactory, upstream: Upstream) -> None:
    """The listener still serves a valid plain request, forwarded without credentials."""
    request = (
        f"GET {upstream.url('/echo')} HTTP/1.1\r\nHost: localhost:{upstream.port}\r\n"
        f"Proxy-Authorization: {proxy_authorization(broker.token())}\r\nConnection: close\r\n\r\n"
    ).encode()
    received = await _send_plain(broker.latest.port, request)
    assert received.startswith(b"HTTP/1.1 200 "), received
    assert upstream.requests[-1].path == "/echo"
    assert "authorization" not in upstream.requests[-1].headers


async def _assert_tunnel_request_injected(broker: BrokerFactory, upstream: Upstream) -> None:
    """The listener still intercepts a valid tunnel request and injects SECRET into it."""
    request = f"GET {_RULE_PREFIX} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    received = await _send_in_tunnel(broker, upstream, request, seconds=10.0)
    assert received.startswith(b"HTTP/1.1 200 "), received
    assert upstream.requests[-1].headers["authorization"] == [f"Bearer {SECRET}"]


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.timeout(180)
@FUZZ_LIVE
@given(edits=_EDITS)
async def test_mutated_plain_requests_never_crash_the_listener_or_carry_credentials(
    broker: BrokerFactory,
    fuzz_broker: EgressBroker,
    http_upstream: Upstream,
    edits: list[tuple[int, int, int, bytes]],
) -> None:
    """A mutated absolute-form request is answered or dropped; secrets are never looked up for plain HTTP.

    The template targets the rule's path, so unmutated it is refused with tls_required.
    """
    token = broker.token()
    template = (
        f"GET http://localhost:{http_upstream.port}{_RULE_PREFIX} HTTP/1.1\r\n"
        f"Host: localhost:{http_upstream.port}\r\n"
        f"Proxy-Authorization: {proxy_authorization(token)}\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    seen_before = len(http_upstream.requests)

    with capture_logs() as logs:
        received = await _send_plain(fuzz_broker.port, _mutate(template, edits))
        await _wait_until_idle(fuzz_broker)

    _assert_no_broker_fault(logs, received)
    assert broker.resolved == []
    _assert_upstream_never_saw_credentials(http_upstream.requests[seen_before:], token)
    await _assert_plain_request_served(broker, http_upstream)


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.timeout(180)
@FUZZ_LIVE
@given(edits=_EDITS)
async def test_mutated_connect_requests_never_crash_the_listener(
    broker: BrokerFactory,
    fuzz_broker: EgressBroker,
    http_upstream: Upstream,
    tls_upstream: Upstream,
    edits: list[tuple[int, int, int, bytes]],
) -> None:
    """A mutated CONNECT head is answered or dropped without a fault, and opens nothing that carries credentials."""
    token = broker.token()
    template = connect_request(f"localhost:{tls_upstream.port}", authorization=proxy_authorization(token))

    with capture_logs() as logs:
        received = await _send_plain(fuzz_broker.port, _mutate(template, edits))

    _assert_no_broker_fault(logs, received)
    assert broker.resolved == []
    assert tls_upstream.requests == []
    await _assert_plain_request_served(broker, http_upstream)


# Paths under the rule's prefix as a client sends them: h11 takes only visible ASCII, so the rest is percent-encoded.
_WIRE_PATHS = st.one_of(
    _TOKEN_SEGMENTS.map(lambda rest: _RULE_PREFIX + "/" + rest),
    st.text(_ALPHABET, max_size=24).map(lambda rest: _RULE_PREFIX + rest),
).map(lambda path: quote(path, safe="".join(map(chr, range(0x21, 0x7F)))))


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.timeout(180)
@FUZZ_LIVE
@given(target=_WIRE_PATHS)
async def test_tunnel_paths_get_the_secret_only_when_they_resolve_under_the_rule_prefix(
    broker: BrokerFactory,
    fuzz_broker: EgressBroker,
    tls_upstream: Upstream,
    target: str,
) -> None:
    """The path invariant end to end: a real upstream only sees SECRET on paths that stay under the rule's prefix."""
    token = broker.token()
    request = f"GET {target} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n".encode()
    seen_before = len(tls_upstream.requests)

    with capture_logs() as logs:
        received = await _send_in_tunnel(broker, tls_upstream, request)
        await _wait_until_idle(fuzz_broker)

    _assert_no_broker_fault(logs, received)
    _assert_upstream_never_saw_credentials(tls_upstream.requests[seen_before:], token)


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.timeout(180)
@FUZZ_LIVE
@given(edits=_EDITS)
async def test_mutated_tunnel_requests_never_crash_the_listener_or_misdirect_the_secret(
    broker: BrokerFactory,
    fuzz_broker: EgressBroker,
    tls_upstream: Upstream,
    edits: list[tuple[int, int, int, bytes]],
) -> None:
    """A mutated request inside an intercepted tunnel never gets SECRET unless it is a clean request for its rule.

    The template is a valid request for the rule's path, so unmutated it is injected. Every request the upstream
    saw with SECRET must carry it once, as the Bearer value, with a Host naming the tunnel host and a path that
    resolves under the rule's prefix on every modeled server. The client never sees SECRET.
    """
    token = broker.token()
    template = f"GET {_RULE_PREFIX} HTTP/1.1\r\nHost: localhost\r\nAccept: */*\r\nConnection: close\r\n\r\n".encode()
    seen_before = len(tls_upstream.requests)

    with capture_logs() as logs:
        received = await _send_in_tunnel(broker, tls_upstream, _mutate(template, edits))
        await _wait_until_idle(fuzz_broker)

    _assert_no_broker_fault(logs, received)
    assert SECRET.encode() not in received
    _assert_upstream_never_saw_credentials(tls_upstream.requests[seen_before:], token)
    await _assert_tunnel_request_injected(broker, tls_upstream)
