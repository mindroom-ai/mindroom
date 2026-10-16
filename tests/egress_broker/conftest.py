"""Shared fixtures for egress broker proxy tests: fake upstreams, a broker factory, and clients."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import os
import socket
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import httpx
import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService
from mindroom.egress_broker.audit import AuditLog
from mindroom.egress_broker.ca import BrokerCA, materialize_ca_bundle
from mindroom.egress_broker.dial import DialPolicy
from mindroom.egress_broker.messages import ScopeInfo
from mindroom.egress_broker.proxy import EgressBroker
from mindroom.egress_broker.rules import EgressRules
from mindroom.egress_broker.secrets import Secret, SecretMissing
from mindroom.egress_broker.tokens import TokenSigner, WorkerClaims
from mindroom.oauth.github import github_oauth_provider
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target
from tests.oauth_test_utils import publish_oauth_credentials
from tests.test_helpers import make_test_ca

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
    from pathlib import Path

    from mindroom.credentials.manager import CredentialsManager
    from mindroom.egress_broker.audit import AuditRecord
    from mindroom.egress_broker.secrets import SecretResult
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, WorkerScope
    from tests.egress_broker.raw_http import Script, UpstreamFactory

type ServeFactory = Callable[..., Awaitable[int]]
type RawProxy = Callable[[int, bytes], Awaitable[RawResponse]]
type ProxyClient = Callable[..., httpx.AsyncClient]

DEFAULT_CLAIMS = WorkerClaims(
    worker_key="worker-alice-code",
    worker_scope="user_agent",
    routing_agent_name="code",
    tenant_id=None,
    account_id=None,
    channel="matrix",
    agent_name="code",
    requester_id="@alice:example.org",
)


def tool_identity(requester_id: str | None, agent_name: str = "code") -> ToolExecutionIdentity:
    """Return the identity of a tool call by `requester_id` on agent `agent_name`, in a Matrix thread."""
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
        tenant_id=None,
        account_id=None,
    )


def worker_target(
    requester_id: str = "@alice:example.org",
    agent_name: str = "code",
    *,
    scope: WorkerScope | None = "user_agent",
) -> ResolvedWorkerTarget:
    """Return the worker target a tool call by `requester_id` on `agent_name` resolves to under `scope`."""
    return resolve_worker_target(
        scope,
        agent_name,
        tool_identity(requester_id, agent_name),
        private_agent_names=frozenset(),
    )


OAUTH_REFRESH_TOKEN = "github-refresh"  # noqa: S105 - test credential


def account_store(requester_id: str) -> ResolvedWorkerTarget:
    """Return the requester's own store, where GitHub connections live whatever the agent's scope."""
    return resolve_worker_target("user", "code", tool_identity(requester_id))


def connect_account(
    manager: CredentialsManager,
    store: ResolvedWorkerTarget,
    token: str,
    *,
    provider: OAuthProvider | None = None,
    **fields: object,
) -> None:
    """Store a `provider` (default GitHub) connection holding `token` in `store`; `fields` override stored fields."""
    provider = provider or github_oauth_provider()
    publish_oauth_credentials(
        provider,
        {
            "token": token,
            "refresh_token": OAUTH_REFRESH_TOKEN,
            "token_uri": provider.token_url,
            "client_id": f"{provider.id}-client-id",
            "scopes": list(provider.scopes),
            "expires_at": 4_102_444_800.0,
            "_source": "oauth",
            "_oauth_provider": provider.id,
            **fields,
        },
        credentials_manager=manager,
        worker_target=store,
    )


@dataclass(frozen=True)
class UpstreamCA:
    """Test CA that signs the fake upstream's certificate, standing in for public roots."""

    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    @property
    def pem(self) -> str:
        """Return the CA certificate in PEM form."""
        return self.cert.public_bytes(serialization.Encoding.PEM).decode()

    def client_context(self) -> ssl.SSLContext:
        """Return a client context that trusts only this CA."""
        return ssl.create_default_context(cadata=self.pem)

    def server_context(
        self,
        directory: Path,
        *,
        name: str = "localhost",
        with_loopback_ip: bool = True,
    ) -> ssl.SSLContext:
        """Issue a certificate for `name` (and 127.0.0.1 unless told not to) and return a server context serving it."""
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        names: list[x509.GeneralName] = [x509.DNSName(name)]
        if with_loopback_ip:
            names.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(self.cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(hours=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            # Python 3.13 default contexts verify strictly and require the key identifiers.
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
            .sign(self.key, hashes.SHA256())
        )
        cert_path = directory / "upstream.pem"
        key_path = directory / "upstream.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cert_path, key_path)
        return context


@pytest.fixture
def upstream_ca() -> UpstreamCA:
    """Return a fresh test CA for the fake TLS upstream."""
    cert, key = make_test_ca("Egress Broker Test Upstream CA")
    return UpstreamCA(cert=cert, key=key)


# Fake upstream routes. Add a handler here to make it available on both `tls_upstream` and `http_upstream`.


def _header_lists(request: web.Request) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for name, value in request.headers.items():
        headers.setdefault(name.lower(), []).append(value)
    return headers


async def _echo(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "method": request.method,
            "path": request.path,
            "query": dict(request.query),
            "headers": _header_lists(request),
        },
    )


async def _ok(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _close(_request: web.Request) -> web.Response:
    response = web.Response(text="closing")
    response.force_close()
    return response


async def _redirect(_request: web.Request) -> web.Response:
    return web.Response(status=302, headers={"Location": "https://other.invalid/"})


async def _stream(request: web.Request) -> web.StreamResponse:
    response = web.StreamResponse()
    await response.prepare(request)
    for index in range(3):
        if index:
            await asyncio.sleep(0.2)
        await response.write(f"chunk-{index}\n".encode())
    await response.write_eof()
    return response


async def _cookie(_request: web.Request) -> web.Response:
    return web.Response(text="ok", headers={"Set-Cookie": "session=upstream-session; Path=/"})


async def _forged_refusal(_request: web.Request) -> web.Response:
    """Answer like a broker refusal, header included, which only the broker may send."""
    return web.Response(status=403, text="upstream says no", headers={"X-MindRoom-Error": "path_not_allowed"})


async def _websocket(request: web.Request) -> web.WebSocketResponse:
    socket = web.WebSocketResponse()
    await socket.prepare(request)
    async for message in socket:
        if message.type == WSMsgType.TEXT:
            await socket.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await socket.send_bytes(message.data)
    return socket


async def _upload(request: web.Request) -> web.Response:
    length = 0
    async for chunk in request.content.iter_any():
        length += len(chunk)
    return web.json_response({"length": length})


def pkt_line(payload: bytes) -> bytes:
    """Frame one pkt-line the way git reads it: the length, prefix included, as four lowercase hex digits."""
    return f"{len(payload) + 4:04x}".encode() + payload


async def _git_info_refs(request: web.Request) -> web.Response:
    """Serve a smart-HTTP ref advertisement only to ``Basic x-access-token:s3cret``, like GitHub."""
    expected = "Basic " + base64.b64encode(b"x-access-token:s3cret").decode()
    if request.headers.get("Authorization") != expected:
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="git"'})
    sha = b"1" * 40
    body = (
        pkt_line(b"# service=git-upload-pack\n")
        + b"0000"
        + pkt_line(sha + b" HEAD\0agent=egress-broker-test\n")
        + pkt_line(sha + b" refs/heads/main\n")
        + b"0000"
    )
    return web.Response(body=body, content_type="application/x-git-upload-pack-advertisement")


UPSTREAM_ROUTES: dict[str, Callable[[web.Request], Awaitable[web.StreamResponse]]] = {
    "/echo": _echo,
    "/ok": _ok,
    "/close": _close,
    "/redirect": _redirect,
    "/stream": _stream,
    "/cookie": _cookie,
    "/forged-refusal": _forged_refusal,
    "/ws": _websocket,
    "/upload": _upload,
    "/repo.git/info/refs": _git_info_refs,
}


@dataclass(frozen=True)
class SeenRequest:
    """One request the fake upstream served: decoded path, raw target, headers (lowercased names), and client port."""

    path: str
    raw_path: str
    headers: dict[str, list[str]]
    peer_port: int


@dataclass
class Upstream:
    """A running fake upstream; ``requests`` lists every request it served, in order."""

    scheme: str
    host: str
    port: int
    requests: list[SeenRequest]

    @property
    def hits(self) -> list[str]:
        """Return the paths of every request served, in order."""
        return [seen.path for seen in self.requests]

    def url(self, path: str) -> str:
        """Return the absolute URL of `path` on this upstream."""
        return f"{self.scheme}://{self.host}:{self.port}{path}"


async def _serve_upstream(ssl_context: ssl.SSLContext | None) -> AsyncIterator[Upstream]:
    seen: list[SeenRequest] = []

    @web.middleware
    async def record_hit(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        assert request.transport is not None
        peer_port = request.transport.get_extra_info("peername")[1]
        seen.append(
            SeenRequest(
                path=request.path,
                raw_path=request.raw_path,
                headers=_header_lists(request),
                peer_port=peer_port,
            ),
        )
        return await handler(request)

    app = web.Application(middlewares=[record_hit])
    for path, route in UPSTREAM_ROUTES.items():
        app.router.add_route("*", path, route)
    runner = web.AppRunner(app, shutdown_timeout=1.0)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
        await site.start()
        port = runner.addresses[0][1]
        yield Upstream(scheme="https" if ssl_context else "http", host="localhost", port=port, requests=seen)
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def tls_upstream(upstream_ca: UpstreamCA, tmp_path: Path) -> AsyncIterator[Upstream]:
    """Serve the fake upstream over TLS on ``localhost`` with a certificate from `upstream_ca`."""
    async for upstream in _serve_upstream(upstream_ca.server_context(tmp_path)):
        yield upstream


@pytest_asyncio.fixture
async def http_upstream() -> AsyncIterator[Upstream]:
    """Serve the fake upstream over plain HTTP on ``localhost``."""
    async for upstream in _serve_upstream(None):
        yield upstream


@pytest_asyncio.fixture
async def upstreams(upstream_ca: UpstreamCA, tmp_path: Path) -> AsyncIterator[UpstreamFactory]:
    """Return ``start(script=None, *, tls=False, ssl_context=None)``, starting a `RawUpstream` on 127.0.0.1.

    Without a script every request gets a short 200; ``tls=True`` serves `upstream_ca`'s ``localhost`` certificate.
    """
    # raw_http imports this module's request helpers, so it is imported here rather than at the top.
    from tests.egress_broker.raw_http import OK, RawUpstream, UpstreamConnection  # noqa: PLC0415

    started: list[tuple[RawUpstream, asyncio.Server]] = []

    async def start(
        script: Script | None = None,
        *,
        tls: bool = False,
        ssl_context: ssl.SSLContext | None = None,
    ) -> RawUpstream:
        upstream = RawUpstream(script=script or (lambda _index: [OK]))
        context = ssl_context or (upstream_ca.server_context(tmp_path) if tls else None)
        server = await asyncio.get_running_loop().create_server(
            lambda: UpstreamConnection(upstream),
            "127.0.0.1",
            0,
            ssl=context,
        )
        upstream.port = server.sockets[0].getsockname()[1]
        started.append((upstream, server))
        return upstream

    yield start
    for upstream, server in started:
        server.close()
        for transport in upstream.transports:
            transport.abort()
        await server.wait_closed()


@contextlib.asynccontextmanager
async def serving(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None] | None],
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> AsyncIterator[int]:
    """Serve `handler` (an `asyncio.start_server` callback) on a loopback port, yielding the port.

    On exit the server is closed and drained, so a test that asserts on what the handler saw can do so after the
    `async with` block, once nothing more can arrive.
    """
    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ssl_context)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@pytest_asyncio.fixture
async def serve() -> AsyncIterator[ServeFactory]:
    """Return ``start(handler, *, ssl_context=None)``: serve `handler` on a loopback port, closed after the test.

    `start` returns the port; `handler` is an `asyncio.start_server` callback taking the stream reader and writer.
    """
    async with contextlib.AsyncExitStack() as stack:

        async def start(
            handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None] | None],
            *,
            ssl_context: ssl.SSLContext | None = None,
        ) -> int:
            return await stack.enter_async_context(serving(handler, ssl_context=ssl_context))

        yield start


@pytest_asyncio.fixture
async def closing_listener(serve: ServeFactory) -> int:
    """Return the port of a loopback server that closes every connection it accepts."""
    return await serve(lambda _reader, writer: writer.close())


@pytest.fixture
def audit(tmp_path: Path) -> Iterator[AuditLog]:
    """Return the audit log every broker built by the `broker` factory writes to."""
    log = AuditLog(tmp_path / "egress_broker" / "requests.sqlite3")
    yield log
    log.close()


@dataclass
class BrokerFactory:
    """Build and start brokers that share one CA, signer, and audit log; tracks them for teardown.

    ``resolved`` lists the service of every secret lookup any of its brokers made, in order.
    """

    ca: BrokerCA
    signer: TokenSigner
    audit: AuditLog
    upstream_ssl_context: ssl.SSLContext
    brokers: list[EgressBroker] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)

    async def __call__(
        self,
        config: EgressBrokerConfig | None = None,
        *,
        secrets: dict[str, str] | None = None,
        resolve_secret: Callable[[WorkerClaims, str, EgressService], SecretResult] | None = None,
        dial_policy: DialPolicy | None = None,
        manage_url: str | None = None,
        scope_info: Callable[[WorkerClaims], ScopeInfo] | None = None,
        max_body_bytes: int = 1 << 30,
        head_timeout: float = 30.0,
        rules_provider: Callable[[WorkerClaims], EgressBrokerConfig | EgressRules] | None = None,
    ) -> EgressBroker:
        """Start a broker on an ephemeral loopback port; `secrets` maps service names to secrets.

        `resolve_secret` replaces the `secrets` lookup, and `rules_provider` replaces the fixed `config`,
        when a test needs a callback that changes, fails, or depends on the requester's claims. It returns a config,
        or `EgressRules` to add user services. Both are plain functions; the broker awaits the lookup.
        Without `scope_info` the requester's scope is personal.
        """
        current = config or EgressBrokerConfig()
        provider = rules_provider or (lambda _claims: current)

        def rules_for(claims: WorkerClaims) -> EgressRules:
            value = provider(claims)
            return value if isinstance(value, EgressRules) else EgressRules.from_config(value)

        stored = dict(secrets or {})
        lookup = resolve_secret or (
            lambda _claims, service, _routed: Secret(stored[service]) if service in stored else SecretMissing()
        )

        async def recording_lookup(claims: WorkerClaims, service: str, routed: EgressService) -> SecretResult:
            self.resolved.append(service)
            return lookup(claims, service, routed)

        broker = EgressBroker(
            ca=self.ca,
            signer=self.signer,
            rules_provider=rules_for,
            resolve_secret=recording_lookup,
            audit=self.audit,
            dial_policy=dial_policy or DialPolicy(allow_loopback=True),
            upstream_ssl_context=self.upstream_ssl_context,
            manage_url=manage_url,
            max_body_bytes=max_body_bytes,
            head_timeout=head_timeout,
            scope_info=scope_info or (lambda _claims: ScopeInfo(shared=False, managers=(), personal_page=True)),
        )
        await broker.start("127.0.0.1", 0)
        self.brokers.append(broker)
        return broker

    @property
    def latest(self) -> EgressBroker:
        """Return the most recently started broker."""
        return self.brokers[-1]

    def token(self, claims: WorkerClaims = DEFAULT_CLAIMS) -> str:
        """Mint a valid proxy token for these claims."""
        return self.signer.mint(claims)


@pytest_asyncio.fixture
async def broker(upstream_ca: UpstreamCA, audit: AuditLog, tmp_path: Path) -> AsyncIterator[BrokerFactory]:
    """Return a factory for started brokers; loopback dialing is allowed so they reach the fake upstreams."""
    factory = BrokerFactory(
        ca=BrokerCA.load_or_create(tmp_path / "egress_broker", key_password=None),
        signer=TokenSigner(b"k" * 32),
        audit=audit,
        upstream_ssl_context=upstream_ca.client_context(),
    )
    yield factory
    for started in factory.brokers:
        await started.close()


@pytest_asyncio.fixture
async def broker_default_policy(broker: BrokerFactory) -> EgressBroker:
    """Return a started broker with the default dial policy, which blocks loopback and private networks."""
    return await broker(dial_policy=DialPolicy())


@pytest_asyncio.fixture
async def proxy_client(broker: BrokerFactory) -> AsyncIterator[Callable[..., httpx.AsyncClient]]:
    """Return ``make(token, *, verify=None)``: an httpx client using the latest broker as its proxy.

    By default the client trusts only the broker CA, as a worker's bundle would for intercepted hosts.
    """
    clients: list[httpx.AsyncClient] = []

    def make(token: str, *, verify: ssl.SSLContext | None = None) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            proxy=f"http://{token}:@127.0.0.1:{broker.latest.port}",
            verify=verify or ssl.create_default_context(cadata=broker.ca.cert_pem),
            trust_env=False,
        )
        clients.append(client)
        return client

    yield make
    for client in clients:
        await client.aclose()


# The refusal bodies several suites expect, as the broker serializes them (texts: `mindroom.egress_broker.messages`).
PROXY_AUTHENTICATION_REQUIRED = {
    "error": "proxy_authentication_required",
    "message": "The sandbox's proxy credentials are missing or expired. Start a new tool call.",
}
MALFORMED = {"error": "bad_request", "message": "The request is malformed and was refused."}
BROKER_ERROR = {
    "error": "broker_error",
    "message": "The credential broker hit an internal error. Retry; if it keeps failing, tell an administrator.",
}
BODY_TOO_LARGE = {"error": "request_body_too_large", "message": "The request body is larger than the broker accepts."}
METHOD_NOT_ALLOWED = {
    "error": "method_not_allowed",
    "message": "TRACE and TRACK requests are refused because the server would echo the request, "
    "injected credential included, back to the agent.",
}
AMBIGUOUS_PATH = {
    "error": "bad_request",
    "message": "The request path is ambiguous (dot segments, //, backslashes, or encoded forms of them) and was refused. "
    "Use a plain path.",
}
LOCALHOST_UNREACHABLE = {"error": "upstream_unreachable", "message": "localhost could not be reached."}
LOCALHOST_BLOCKED = {
    "error": "destination_blocked",
    "message": "localhost resolves to a private or internal address that the administrator's egress policy does not let agents reach.",
}
# The links a broker started with `manage_url=MANAGE_URL`, or a lookup returning `CONNECT_URL`, puts in refusals.
MANAGE_URL = "https://m.test/connections/egress"
CONNECT_URL = "https://m.test/api/oauth/github/authorize?connect_token=one-time-token"


def host_not_allowed(*services: str) -> dict[str, object]:
    """Return the refusal body for ``localhost`` under a deny policy whose operator services are `services`."""
    return {
        "error": "host_not_allowed",
        "services": list(services),
        "message": "localhost is not allowed by the administrator's egress policy.",
    }


def single_rule_config(
    host: str = "localhost",
    *,
    path_prefix: str = "/",
    auth: EgressAuth | None = None,
    restrict_to_rules: bool = False,
    unmatched_hosts: Literal["passthrough", "deny"] = "passthrough",
) -> EgressBrokerConfig:
    """Return a config with one service, ``svc``, whose one rule covers `path_prefix` on `host` (bearer by default)."""
    rule = EgressRule(host=host, path_prefix=path_prefix, auth=auth or EgressAuth(type="bearer"))
    return EgressBrokerConfig(
        unmatched_hosts=unmatched_hosts,
        services={"svc": EgressService(restrict_to_rules=restrict_to_rules, rules=[rule])},
    )


def with_user_services(operator: EgressBrokerConfig, **user: EgressService) -> EgressRules:
    """Return the operator's config merged with one scope's user services, as the broker builds them."""
    effective = operator.model_copy(update={"services": {**operator.services, **user}})
    return EgressRules(operator=operator, effective=effective)


def free_port() -> int:
    """Return a loopback port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def proxy_authorization(token: str) -> str:
    """Return the ``Proxy-Authorization`` value a worker sends: Basic with the token as user name."""
    return "Basic " + base64.b64encode(f"{token}:".encode()).decode()


def plain_request(target: str, *, authorization: str, path: str = "/", method: str = "GET") -> bytes:
    """Return a raw absolute-form `method` request for `path` on `target` (host:port), as a worker's plain client sends."""
    return (
        f"{method} http://{target}{path} HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: {authorization}\r\n\r\n"
    ).encode()


def connect_request(target: str, *, authorization: str | None = None) -> bytes:
    """Return a raw CONNECT request head for `target`, optionally with a Proxy-Authorization value."""
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
    if authorization is not None:
        lines.append(f"Proxy-Authorization: {authorization}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def audit_records(audit: AuditLog, count: int) -> list[AuditRecord]:
    """Wait for the broker to write `count` records; tunnels are audited only after they close."""
    async with asyncio.timeout(5):
        # The broker writes from its own thread, so the database is the only thing to watch.
        while len(records := audit.query()) < count:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    return records


async def assert_audited(audit: AuditLog, **expected: object) -> AuditRecord:
    """Wait for the one audit record, check each field named in `expected` equals it exactly, and return the record."""
    [record] = await audit_records(audit, 1)
    assert {name: getattr(record, name) for name in expected} == expected
    return record


@dataclass(frozen=True)
class RawResponse:
    """A response head (and Content-Length body) read straight off a proxy socket."""

    status: int
    headers: dict[str, str]
    body: bytes
    reason: str = ""

    def json(self) -> object:
        """Decode the body as JSON."""
        return json.loads(self.body)


def parse_head(head: bytes) -> tuple[int, str, dict[str, str]]:
    """Return the status, reason phrase, and headers (lowercased names) of a raw response head."""
    lines = head.decode("latin-1").removesuffix("\r\n\r\n").split("\r\n")
    _version, status, reason = (*lines[0].split(" ", 2), "")[:3]
    headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines[1:])}
    return int(status), reason, headers


async def read_raw_response(reader: asyncio.StreamReader) -> RawResponse:
    """Read one response head and its Content-Length body (none for a CONNECT 200) from a raw socket."""
    async with asyncio.timeout(10):
        status, reason, headers = parse_head(await reader.readuntil(b"\r\n\r\n"))
        body = await reader.readexactly(int(headers.get("content-length", "0")))
    return RawResponse(status=status, headers=headers, body=body, reason=reason)


async def _raw_proxy_request(port: int, request: bytes) -> RawResponse:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        return await read_raw_response(reader)
    finally:
        writer.transport.abort()


@pytest.fixture
def raw_proxy() -> Callable[[int, bytes], Awaitable[RawResponse]]:
    """Return ``send(port, request_bytes)`` for proxy requests that httpx cannot express or inspect."""
    return _raw_proxy_request


_PROXY_ENV = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "git_askpass", "ssh_askpass"}


def env_without_proxies(**extra: str) -> dict[str, str]:
    """Return this process's environment without proxy or askpass settings that would bypass the broker."""
    return {name: value for name, value in os.environ.items() if name.lower() not in _PROXY_ENV} | extra


async def run_command(*command: str, env: dict[str, str]) -> tuple[int, str, str]:
    """Run a command line tool while the broker keeps serving on this event loop; return its status, stdout, stderr."""
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    async with asyncio.timeout(30):
        stdout, stderr = await process.communicate()
    assert process.returncode is not None
    return process.returncode, stdout.decode(), stderr.decode()


async def git_ls_remote(broker: BrokerFactory, url: str, tmp_path: Path) -> tuple[int, str, str]:
    """Run ``git ls-remote url`` through the latest broker with only its proxy URL and trust bundle configured.

    No credentials are set anywhere, and the broker CA bundle is written under `tmp_path`.
    """
    bundle, _ca_only = materialize_ca_bundle(broker.ca.cert_pem, tmp_path / "trust")
    return await run_command(
        "git",
        "-c",
        f"http.proxy=http://{broker.token()}:@127.0.0.1:{broker.latest.port}",
        "-c",
        f"http.sslCAInfo={bundle}",
        "-c",
        "credential.helper=",
        "ls-remote",
        url,
        env=env_without_proxies(
            HOME=str(tmp_path),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
        ),
    )
